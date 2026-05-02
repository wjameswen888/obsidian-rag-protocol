#!/usr/bin/env python3
"""
rebuild-vault-index.py — Reference implementation of the Obsidian RAG Protocol.

Scans configured Obsidian vault subdirectories for .md files,
builds a machine-readable JSON index with SHA256-based incremental updates.

USAGE:
    python3 rebuild-vault-index.py \
        --vault ~/Documents/MyVault \
        --output ~/.hermes/vault-index.json \
        --scan wiki/projects wiki/career hermes-knowledge/

KEY DESIGN DECISIONS:
- SHA256 content hashing (not mtime — unreliable on macOS/iCloud)
- Path-based exclusion rules (not frontmatter-based — more deterministic)
- Frontmatter-driven summaries with plain-text fallback
- Aliases preserved from previous run unless frontmatter updates them
- Incremental: same hash → skip extraction → preserve all metadata
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════
# CONFIGURATION — Edit these for your vault
# ═══════════════════════════════════════════════════

# Excluded directories (relative to vault root or any scan root)
EXCLUDE_DIRS = {
    "archived", "archive", "log", "modes",
    "tracking", "task-",
}

# Excluded filename patterns (regex, matched against stem)
EXCLUDE_FILENAME_PATTERNS = [
    r"^plan-",              # plan-*.md
    r"-progress$",          # *-progress.md
    r"-data-\d{4}-\d{2}$",  # *-data-YYYY-MM.md
]

# Files to always skip
SKIP_FILES = {"index.md", "README.md"}

# Default cutoff: skip files older than this many days
DEFAULT_CUTOFF_DAYS = 90


# ═══════════════════════════════════════════════════
# FRONTMATTER EXTRACTION
# ═══════════════════════════════════════════════════

def extract_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown text."""
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}

    fm = {}
    in_list = False
    list_key = None
    list_vals = []

    for line in match.group(1).split("\n"):
        # Handle inline list: key: [a, b, c]
        inline_list = re.match(r"^(\w[\w_-]*):\s*\[(.*)\]", line)
        if inline_list:
            key = inline_list.group(1)
            vals = [v.strip().strip('"').strip("'")
                    for v in inline_list.group(2).split(",") if v.strip()]
            fm[key] = vals
            continue

        # Handle multi-line list
        if in_list:
            list_match = re.match(r"^\s+-\s+(.+)", line)
            if list_match:
                list_vals.append(list_match.group(1).strip().strip('"').strip("'"))
                continue
            else:
                fm[list_key] = list_vals
                in_list = False
                list_key = None
                list_vals = []

        # Simple key: value
        kv = re.match(r"^(\w[\w_-]*):\s*(.+)", line)
        if kv:
            key = kv.group(1)
            val = kv.group(2).strip().strip('"').strip("'")
            if val.startswith("- "):
                in_list = True
                list_key = key
                list_vals = [val[2:].strip().strip('"').strip("'")]
            else:
                fm[key] = val

    if in_list and list_key:
        fm[list_key] = list_vals

    return fm


# ═══════════════════════════════════════════════════
# CONTENT HASHING
# ═══════════════════════════════════════════════════

def file_hash(filepath: Path) -> str:
    """SHA256 of file content. Reliable on macOS even with iCloud sync."""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()


# ═══════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════

def should_index(filepath: Path, scan_root: Path) -> bool:
    """Single entry point for all exclusion logic."""
    if filepath.name in SKIP_FILES:
        return False

    for parent in filepath.relative_to(scan_root).parents:
        if parent.name in EXCLUDE_DIRS:
            return False
    for parent in filepath.parents:
        if parent.name in EXCLUDE_DIRS:
            return False

    stem = filepath.stem
    for pattern in EXCLUDE_FILENAME_PATTERNS:
        if re.match(pattern, stem):
            return False

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return False
    if content.startswith("---"):
        fm = extract_frontmatter(content)
        if fm.get("rag_exclude") in ("true", "True", True, "yes"):
            return False

    return True


def get_first_paragraph(text: str) -> str:
    """Extract first non-empty, non-heading, non-table line after frontmatter."""
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", ">", "|", "<!--")):
            continue
        return stripped[:150] + ("..." if len(stripped) > 150 else "")
    return ""


def extract_summary(fm: dict, body: str) -> dict:
    """Build structured summary from frontmatter, with plain-text fallback."""
    summary = {}

    summary["status"] = fm.get("status", "unknown")

    if "version" in fm:
        summary["version"] = fm["version"]

    if "summary_points" in fm and isinstance(fm["summary_points"], list):
        summary["key_points"] = fm["summary_points"][:5]
    else:
        first_para = get_first_paragraph(body)
        if first_para:
            summary["key_points"] = [first_para]

    if "last_action" in fm:
        summary["last_action"] = fm["last_action"]

    return summary


def resolve_aliases(
    entry_id: str,
    fm: dict,
    old_aliases: Optional[list],
    alias_maps: dict
) -> list:
    """Resolve aliases for an entry.

    Priority:
    1. Frontmatter `aliases` field (explicit update)
    2. Hand-curated alias_maps entry
    3. Preserve from previous index run
    4. Fallback: [entry_id, title.lower()]
    """
    # Frontmatter aliases (scalar tolerance: wrap string as list)
    if "aliases" in fm:
        aliases_val = fm["aliases"]
        if isinstance(aliases_val, list) and aliases_val:
            return aliases_val
        elif isinstance(aliases_val, str) and aliases_val.strip():
            return [aliases_val.strip()]

    # Hand-curated
    if entry_id in alias_maps:
        return alias_maps[entry_id]

    # Preserve from previous run
    if old_aliases:
        return old_aliases

    # Fallback
    title = fm.get("title", entry_id.replace("-", " "))
    return [entry_id, title.lower().replace(" ", "-")]


# ═══════════════════════════════════════════════════
# SCAN & REBUILD
# ═══════════════════════════════════════════════════

def load_old_index(output_path: Path) -> dict:
    """Load previous index for incremental comparison."""
    if not output_path.exists():
        return {"entries": {}, "updated": "1970-01-01"}
    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": {}, "updated": "1970-01-01"}


def scan_directory(
    directory: Path,
    author: str,
    old_entries: dict,
    alias_maps: dict,
    cutoff_days: int = DEFAULT_CUTOFF_DAYS,
) -> list:
    """Scan directory for indexable files. Returns list of entry dicts."""
    entries = []
    if not directory.exists():
        return entries

    cutoff = datetime.now() - timedelta(days=cutoff_days)

    for md_file in sorted(directory.rglob("*.md")):
        if not should_index(md_file, directory):
            continue

        mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
        if mtime < cutoff:
            continue

        content_hash = file_hash(md_file)

        entry_id = md_file.stem.lower().replace(" ", "-").replace("_", "-")

        # Handle name collisions: prefix with parent dir
        collision_count = sum(
            1 for e in entries
            if Path(e["path"]).stem.lower().replace(" ", "-").replace("_", "-") == entry_id
        )
        if collision_count > 0:
            parent_dir = md_file.parent.name.lower().replace(" ", "-").replace("_", "-")
            entry_id = f"{parent_dir}-{entry_id}"

        old_entry = old_entries.get(entry_id)

        # Incremental skip
        if old_entry and old_entry.get("_content_hash") == content_hash:
            entries.append(old_entry)
            continue

        # Full extraction for new/changed files
        content = md_file.read_text(encoding="utf-8")
        fm = extract_frontmatter(content) if content.startswith("---") else {}

        title = fm.get("title", md_file.stem.replace("-", " ").title())
        updated = fm.get("updated", mtime.strftime("%Y-%m-%d"))
        entry_author = fm.get("author", author)

        summary = extract_summary(fm, content)

        old_aliases = old_entry.get("aliases") if old_entry else None
        aliases = resolve_aliases(entry_id, fm, old_aliases, alias_maps)

        entries.append({
            "_content_hash": content_hash,
            "path": str(md_file),
            "title": title,
            "summary": summary,
            "updated": updated,
            "author": entry_author,
            "aliases": aliases,
        })

    return entries


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Rebuild vault-index.json for Obsidian RAG Protocol"
    )
    parser.add_argument(
        "--vault", required=True,
        help="Path to Obsidian vault root"
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to output vault-index.json"
    )
    parser.add_argument(
        "--scan", nargs="+", required=True,
        help="Subdirectories to scan, format: DIR:AUTHOR or DIR"
    )
    parser.add_argument(
        "--cutoff-days", type=int, default=DEFAULT_CUTOFF_DAYS,
        help=f"Skip files older than N days (default: {DEFAULT_CUTOFF_DAYS})"
    )
    parser.add_argument(
        "--alias-maps", type=str, default=None,
        help="Path to a JSON file with hand-curated alias mappings"
    )

    args = parser.parse_args()

    vault_root = Path(args.vault).expanduser().resolve()
    if not vault_root.exists():
        print(f"ERROR: Vault not found: {vault_root}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse scan directories
    scan_targets = []
    for spec in args.scan:
        if ":" in spec:
            path_str, author = spec.rsplit(":", 1)
        else:
            path_str, author = spec, "unknown"
        scan_dir = vault_root / path_str
        scan_targets.append((scan_dir, author))

    # Load alias maps
    alias_maps = {}
    if args.alias_maps:
        try:
            with open(args.alias_maps) as f:
                alias_maps = json.load(f)
        except Exception as e:
            print(f"WARNING: Could not load alias maps: {e}", file=sys.stderr)

    old_index = load_old_index(output_path)
    old_entries = old_index.get("entries", {})

    new_entries = {}

    for directory, author in scan_targets:
        for entry in scan_directory(
            directory, author, old_entries, alias_maps,
            cutoff_days=args.cutoff_days
        ):
            entry_id = Path(entry["path"]).stem.lower().replace(" ", "-").replace("_", "-")
            new_entries[entry_id] = entry

    index = {
        "updated": datetime.now().strftime("%Y-%m-%d"),
        "entries": new_entries,
    }

    output_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    changed = sum(
        1 for kid, v in new_entries.items()
        if old_entries.get(kid, {}).get("_content_hash") != v.get("_content_hash", "__new__")
    )
    print(f"✅ vault-index.json rebuilt: {len(new_entries)} entries ({changed} changed)")


if __name__ == "__main__":
    main()
