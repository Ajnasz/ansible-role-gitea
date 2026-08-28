#!/usr/bin/env python3
"""Sync roles/ajnasz.gitea/defaults/main.yml and templates/app.ini.j2 against
upstream gitea's custom/conf/app.example.ini.

Downloads app.example.ini for a given gitea branch/tag, finds every
[section]/KEY pair that isn't already represented in templates/app.ini.j2,
and appends the missing ones to both defaults/main.yml (as `var: ~` - null
by default, so gitea's own built-in default applies) and templates/app.ini.j2
(as a `{% if var is not none %}KEY = {{ var }}{% endif %}` block, so the key
is only emitted into app.ini when a caller actually overrides it). This way
we don't have to keep hand-maintained copies of gitea's defaults in sync.

Usage:
    scripts/sync_app_ini.py [--branch release/v1.26] [--dry-run]

With no --branch, the branch is derived from defaults/main.yml's gitea_version
(e.g. 1.26.4 -> release/v1.26).
"""
import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROLE_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = ROLE_DIR / "defaults" / "main.yml"
TEMPLATE_FILE = ROLE_DIR / "templates" / "app.ini.j2"

RAW_URL = "https://raw.githubusercontent.com/go-gitea/gitea/{branch}/custom/conf/app.example.ini"

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
COMMENTED_SECTION_RE = re.compile(r"^;\[([^\]]+)\]\s*$")
KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*=\s*(.*?)\s*$")
COMMENTED_KEY_RE = re.compile(r"^;([A-Za-z0-9_.\-]+)\s*=\s*(.*?)\s*$")

ROOT_SECTION = ""  # keys that appear before the first [section] header


def fetch_ini(branch: str) -> str:
    url = RAW_URL.format(branch=branch)
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def branch_from_version(version: str) -> str:
    major, minor = version.split(".")[:2]
    return f"release/v{major}.{minor}"


def parse_upstream(text: str):
    """Return {section_name: [(key, default_value), ...]} in file order,
    skipping dynamic/templated section or key names (containing '%')."""
    sections: dict[str, list[tuple[str, str]]] = {ROOT_SECTION: []}
    seen: dict[str, set[str]] = {ROOT_SECTION: set()}
    current = ROOT_SECTION

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")

        m = SECTION_RE.match(line) or COMMENTED_SECTION_RE.match(line)
        if m:
            name = m.group(1)
            if "%" in name:
                # dynamic section name (e.g. log.%(WriterMode)) - not a real section
                current = None
                continue
            current = name
            sections.setdefault(current, [])
            seen.setdefault(current, set())
            continue

        if current is None:
            continue

        m = KEY_RE.match(line) or COMMENTED_KEY_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2)
            # strip inline `; comment` trailers - gitea's example ini uses `;`
            # both to disable a whole line and to trail an inline comment
            # after a real value (e.g. `;PATH = ; Will default to ...`)
            value = value.split(";", 1)[0].strip()
            if "%" in key:
                continue
            if key in seen[current]:
                continue
            seen[current].add(key)
            sections[current].append((key, value))

    return sections


def parse_template_coverage(text: str):
    """Return {section_name: set(KEY, ...)} already present in app.ini.j2."""
    coverage: dict[str, set[str]] = {ROOT_SECTION: set()}
    current = ROOT_SECTION
    key_line_re = re.compile(r"^([A-Za-z0-9_.\-]+)\s*[:=]")

    for line in text.splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            current = m.group(1)
            coverage.setdefault(current, set())
            continue
        m = key_line_re.match(line.strip())
        if m:
            coverage.setdefault(current, set()).add(m.group(1))

    return coverage


def parse_existing_var_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z0-9_]+):", line)
        if m:
            names.add(m.group(1))
    return names


def slug(name: str) -> str:
    return re.sub(r"[.\-\s]+", "_", name).lower()


def var_name_for(section: str, key: str) -> str:
    # keys can themselves contain dots/dashes (git config keys like
    # `core.quotepath`, mimetype extensions like `.apk`, header names like
    # `Reply-To`) - slug them too so the result is a flat, valid identifier
    # (an un-slugged dot would parse as Jinja attribute access, not a var).
    key_slug = slug(key)
    if section == ROOT_SECTION:
        return f"gitea_{key_slug}"
    return f"gitea_{slug(section)}_{key_slug}"


def infer_kind(value: str) -> str:
    """Return 'bool' if the upstream default looks boolean, else 'other'.
    Only used to decide whether the template needs a `|lower` filter -
    the ansible default itself is always null (see module docstring)."""
    if value.lower() in ("true", "false"):
        return "bool"
    return "other"


def yaml_comment_for(value: str) -> str:
    if value == "":
        return ""
    escaped = value.replace("\\", "\\\\")
    return f"  # gitea default: {escaped}"


def build_additions(upstream, coverage, existing_var_names, gitea_version):
    """Return (defaults_block: str, template_additions: dict[section] -> list[str] lines,
    new_sections_order: list[str])."""
    defaults_lines = [
        "",
        f"# --- auto-synced from app.example.ini (gitea {gitea_version}) ---",
    ]
    template_additions: dict[str, list[str]] = {}
    section_vars: dict[str, list[str]] = {}
    new_sections_order: list[str] = []
    used_names = set(existing_var_names)
    total_new_keys = 0

    for section, keys in upstream.items():
        covered = coverage.get(section, set())
        missing = [(k, v) for k, v in keys if k not in covered]
        if not missing:
            continue
        total_new_keys += len(missing)

        is_new_section = section not in coverage
        if is_new_section:
            new_sections_order.append(section)

        section_default_lines = []
        section_template_lines = []
        section_vars[section] = []
        for key, value in missing:
            var = var_name_for(section, key)
            if var in used_names:
                suffix = 2
                while f"{var}_{suffix}" in used_names:
                    suffix += 1
                var = f"{var}_{suffix}"
            used_names.add(var)
            section_vars[section].append(var)

            kind = infer_kind(value)
            section_default_lines.append(f"{var}: ~{yaml_comment_for(value)}")

            rendered_value = f"{{{{ {var}|lower }}}}" if kind == "bool" else f"{{{{ {var} }}}}"
            section_template_lines.append(f"{{% if {var} is not none %}}")
            section_template_lines.append(f"{key} = {rendered_value}")
            section_template_lines.append("{% endif %}")

        defaults_lines.append(f"# --- [{section}] ---" if section != ROOT_SECTION else "# --- (root) ---")
        defaults_lines.extend(section_default_lines)
        defaults_lines.append("")

        if is_new_section:
            # apply_template_additions wraps the whole section (including this
            # blank) in its own outer conditional and strips this marker back out
            section_template_lines.append("")
        else:
            # existing section: only add the blank separator if something in
            # *this* block actually rendered, so an all-null section doesn't
            # leave a bare extra blank line before the next [section] header
            outer_condition = " or ".join(f"{v} is not none" for v in section_vars[section])
            section_template_lines.append(f"{{% if {outer_condition} %}}")
            section_template_lines.append("")
            section_template_lines.append("{% endif %}")

        template_additions[section] = section_template_lines

    return "\n".join(defaults_lines).rstrip() + "\n", template_additions, new_sections_order, total_new_keys, section_vars


def apply_template_additions(template_text: str, template_additions: dict, new_sections_order: list, section_vars: dict):
    lines = template_text.splitlines()
    out = []
    current = ROOT_SECTION
    section_starts = {}  # section -> index in `out` where its header line lives, for root track membership only

    i = 0
    n = len(lines)
    # We rebuild line by line, and whenever we detect we're about to leave a
    # section (next section header or EOF) and that section has additions
    # pending, we splice them in just before leaving.
    pending = dict(template_additions)  # sections present in template with new keys

    def flush_section(target_list, section_name):
        if section_name in pending and pending[section_name]:
            target_list.extend(pending.pop(section_name))

    while i < n:
        line = lines[i]
        m = SECTION_RE.match(line.strip())
        if m:
            # leaving `current`, entering new section
            flush_section(out, current)
            current = m.group(1)
            out.append(line)
        else:
            out.append(line)
        i += 1

    # EOF reached: flush whatever section we ended in
    flush_section(out, current)

    # brand new sections not present in the template at all - wrap the whole
    # section (header included) in an outer condition, so an unused section
    # doesn't leave a bare `[section]` stanza with no keys in app.ini
    for section in new_sections_order:
        add_lines = template_additions.get(section)
        if not add_lines:
            continue
        # the blank separator line lives *inside* the conditional too, so a
        # section that ends up fully empty (nothing overridden) contributes
        # no output at all instead of an orphaned blank line.
        outer_condition = " or ".join(f"{v} is not none" for v in section_vars[section])
        out.append(f"{{% if {outer_condition} %}}")
        out.append("")
        out.append(f"[{section}]")
        out.extend(l for l in add_lines if l != "")
        out.append("{% endif %}")

    return "\n".join(out).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", help="gitea branch/tag to fetch app.example.ini from, e.g. release/v1.26")
    parser.add_argument("--dry-run", action="store_true", help="print a summary instead of writing files")
    args = parser.parse_args()

    defaults_text = DEFAULTS_FILE.read_text()
    template_text = TEMPLATE_FILE.read_text()

    version_m = re.search(r'^gitea_version:\s*"([^"]+)"', defaults_text, re.M)
    gitea_version = version_m.group(1) if version_m else "unknown"
    branch = args.branch or branch_from_version(gitea_version)

    print(f"Fetching app.example.ini from branch '{branch}' (gitea_version={gitea_version})...", file=sys.stderr)
    ini_text = fetch_ini(branch)

    upstream = parse_upstream(ini_text)
    coverage = parse_template_coverage(template_text)
    existing_var_names = parse_existing_var_names(defaults_text)

    defaults_block, template_additions, new_sections_order, total_new_keys, section_vars = build_additions(
        upstream, coverage, existing_var_names, gitea_version
    )

    total_new_sections = len(new_sections_order)
    print(f"{total_new_keys} new keys across {len(template_additions)} sections "
          f"({total_new_sections} brand-new sections).", file=sys.stderr)

    if args.dry_run:
        print(defaults_block)
        for section, add_lines in template_additions.items():
            print(f"\n[{section}]" if section != ROOT_SECTION else "\n(root)")
            for l in add_lines:
                print(f"  {l}")
        return

    DEFAULTS_FILE.write_text(defaults_text.rstrip("\n") + "\n" + defaults_block)
    TEMPLATE_FILE.write_text(
        apply_template_additions(template_text, template_additions, new_sections_order, section_vars)
    )
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
