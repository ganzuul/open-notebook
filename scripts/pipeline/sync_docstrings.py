#!/usr/bin/env python3
"""
Bidirectional sync between Lean4 docstrings and Open Notebook notes.

Directions:
  --extract     Parse /--, /-!, /- docstring blocks from .lean files
                and create/update "Docstrings" notes in ON.
  --inject      Read Deep Analysis notes from ON, inject as module-level
                /- block comments into .lean files.
  --check       Show what would change without writing anything.

The --inject direction is the primary value: it pushes the 35B-generated
Deep Analysis notes back into the source code as rich module docstrings.

The --extract direction is for discoverability: it makes inline docstrings
searchable in the ON web UI alongside the Deep Analysis.
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import requests

ON_URL = "http://localhost:5055/api"
LASERCORTEX_DIR = "/home/nos/labware/LaserCortex"

NOTEBOOK_NAME = "LaserCortex"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api(method, path, data=None, timeout=30):
    url = f"{ON_URL}{path}"
    try:
        if method == "get":
            r = requests.get(url, timeout=timeout)
        elif method == "post":
            r = requests.post(url, json=data, timeout=timeout)
        elif method == "put":
            r = requests.put(url, json=data, timeout=timeout)
        elif method == "delete":
            r = requests.delete(url, timeout=timeout)
        else:
            return None
        return r
    except requests.RequestException as e:
        print(f"  [API error] {e}")
        return None


def find_notebook_id(name=NOTEBOOK_NAME):
    r = api("get", "/notebooks")
    if not r or r.status_code != 200:
        return None
    for nb in r.json():
        if nb["name"] == name:
            return nb["id"]
    return None


def find_notes_by_title(title_prefix):
    """Return all notes whose title starts with title_prefix."""
    r = api("get", "/notes")
    if not r or r.status_code != 200:
        return []
    notes = r.json() if isinstance(r.json(), list) else r.json().get("notes", r.json().get("data", []))
    return [n for n in notes if n.get("title", "").startswith(title_prefix)]


def find_deep_analysis_notes():
    """Return Deep Analysis notes (title ends with ' — Deep Analysis')."""
    r = api("get", "/notes")
    if not r or r.status_code != 200:
        return []
    notes = r.json() if isinstance(r.json(), list) else r.json().get("notes", r.json().get("data", []))
    return [n for n in notes if n.get("title", "").endswith(" — Deep Analysis")]


def find_semantic_index_note(module_name):
    """Find the Semantic Index note for a given module name."""
    r = api("get", "/notes")
    if not r or r.status_code != 200:
        return None
    notes = r.json() if isinstance(r.json(), list) else r.json().get("notes", r.json().get("data", []))
    for n in notes:
        if n.get("title") == f"{module_name} — Semantic Index":
            return n
    return None


def get_note_content(note_id):
    r = api("get", f"/notes/{note_id}")
    if not r or r.status_code != 200:
        return None
    return r.json().get("content", "")


def create_note(notebook_id, title, content, note_type="ai"):
    r = api("post", "/notes", data={
        "notebook_id": notebook_id,
        "title": title,
        "content": content,
        "note_type": note_type,
    })
    if r and r.status_code in (200, 201):
        result = r.json()
        print(f"  Created note: {result.get('id', '?')} ({title})")
        return result.get("id")
    else:
        body = r.text[:200] if r else "no response"
        print(f"  ERROR creating note: {body}")
        return None


def update_note_content(note_id, content):
    r = api("put", f"/notes/{note_id}", data={"content": content})
    if r and r.status_code == 200:
        print(f"  Updated note: {note_id}")
        return True
    else:
        body = r.text[:200] if r else "no response"
        print(f"  ERROR updating note: {body}")
        return False


# ---------------------------------------------------------------------------
# Lean docstring parser
# ---------------------------------------------------------------------------

def find_docstring_blocks(text):
    """Find all docstring blocks in Lean source text.

    Returns list of dicts with keys:
        kind, start, end, content, decl_name, decl_type, line_start
    """
    blocks = []
    i = 0
    while i < len(text):
        # Determine comment kind
        if text[i:i+3] == '/--':
            kind = 'decl_doc'
            start = i
            i += 3
        elif text[i:i+3] == '/-!':
            kind = 'module_doc'
            start = i
            i += 3
        elif text[i:i+2] == '/-':
            kind = 'block_comment'
            start = i
            i += 2
        else:
            i += 1
            continue

        # Find closing -/
        end = text.find('-/', i)
        if end == -1:
            break  # Unterminated
        end += 2

        # Extract content (strip the opening markers)
        if kind == 'block_comment':
            raw = text[start+2:end-2]
        else:
            raw = text[start+3:end-2]
        content = raw.strip()

        # For decl_doc, peek ahead for the declaration name
        decl_name = None
        decl_type = None
        if kind == 'decl_doc':
            after = text[end:end+300]
            m = re.search(r'\b(def|theorem|inductive|structure|abbrev|instance|class)\s+(\w+)', after)
            if m:
                decl_name = m.group(2)
                decl_type = m.group(1)

        blocks.append({
            'kind': kind,
            'start': start,
            'end': end,
            'content': content,
            'decl_name': decl_name,
            'decl_type': decl_type,
            'line_start': text[:start].count('\n') + 1,
        })
        i = end

    return blocks


def lean_file_paths(base_dir):
    """Yield absolute paths to all .lean files under base_dir, skipping .lake/."""
    for root, dirs, files in os.walk(base_dir):
        # Skip hidden directories and .lake (mathlib4 dependencies)
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'lake-packages']
        for f in files:
            if f.endswith(".lean"):
                yield os.path.join(root, f)


def module_name_from_path(path, base_dir):
    """Convert /path/to/LaserCortex/Foo.lean → 'Foo'"""
    rel = os.path.relpath(path, base_dir)
    # Strip extension and leading directory
    rel = rel.replace(os.sep, '/')
    # Remove .lean
    if rel.endswith('.lean'):
        rel = rel[:-5]
    # If it starts with LaserCortex/, strip that prefix
    parts = rel.split('/')
    if parts and parts[0] == 'LaserCortex':
        parts = parts[1:]
    return '.'.join(parts) if parts else rel


def docstrings_hash(blocks):
    """SHA256 prefix of all docstring content for change detection."""
    h = hashlib.sha256()
    for b in blocks:
        key = b.get('decl_name') or b.get('kind', '?')
        h.update(f"{key}:{b['content']}".encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Direction: Docstrings → ON Notes (--extract)
# ---------------------------------------------------------------------------

def format_docstring_note_content(module_name, file_path, blocks, content_hash):
    """Build the markdown content for a Docstrings note."""
    lines = [
        f"## Module: {module_name} — Docstrings",
        f"**File**: {file_path}",
        f"**Hash**: `{content_hash}`",
        "",
    ]

    # Module-level docs
    module_blocks = [b for b in blocks if b['kind'] == 'module_doc']
    if module_blocks:
        lines.append("### Module-level documentation")
        lines.append("")
        for b in module_blocks:
            lines.append(b['content'])
            lines.append("")

    # Declaration docs
    decl_blocks = [b for b in blocks if b['kind'] == 'decl_doc']
    if decl_blocks:
        lines.append("### Declarations")
        lines.append("")
        for b in decl_blocks:
            name = b.get('decl_name', '?')
            dtype = b.get('decl_type', '?')
            lines.append(f"**`{dtype} {name}`** (line {b['line_start']}):")
            lines.append("")
            for line in b['content'].split('\n'):
                lines.append(f"  {line.strip()}")
            lines.append("")

    # Block comments (only include substantial ones at module level)
    # Skip — these are usually too long and unstructured

    return '\n'.join(lines)


def do_extract(notebook_id, lean_dir, dry_run=False):
    if not notebook_id:
        print("ERROR: Notebook not found")
        return False

    print(f"Extracting docstrings from {lean_dir} ...")
    lean_files = list(lean_file_paths(lean_dir))
    print(f"  Found {len(lean_files)} .lean files")

    changed = 0
    created = 0
    unchanged = 0
    errors = 0

    for fpath in lean_files:
        try:
            with open(fpath) as f:
                text = f.read()
        except Exception as e:
            print(f"  ERROR reading {fpath}: {e}")
            errors += 1
            continue

        blocks = find_docstring_blocks(text)
        if not blocks:
            # File has no docstrings at all, skip
            unchanged += 1
            continue

        mod_name = module_name_from_path(fpath, lean_dir)
        content_hash = docstrings_hash(blocks)
        note_content = format_docstring_note_content(mod_name, fpath, blocks, content_hash)
        note_title = f"{mod_name} — Docstrings"

        # Check if note already exists
        existing = find_notes_by_title(note_title)
        # exact match
        exact = [n for n in existing if n.get("title") == note_title]

        if exact:
            note = exact[0]
            existing_content = get_note_content(note["id"])
            if existing_content:
                # Check hash embedded in the note
                hash_match = re.search(r'\*\*Hash\*\*: `([^`]+)`', existing_content)
                if hash_match and hash_match.group(1) == content_hash:
                    unchanged += 1
                    continue
            # Content changed
            if not dry_run:
                update_note_content(note["id"], note_content)
            changed += 1
            if dry_run:
                print(f"  [dry-run] WOULD UPDATE: {note_title}")
        else:
            if not dry_run:
                create_note(notebook_id, note_title, note_content)
            created += 1
            if dry_run:
                print(f"  [dry-run] WOULD CREATE: {note_title}")

    total = changed + created + unchanged
    print(f"  Extracted: {total} files, {created} created, {changed} updated, {unchanged} unchanged"
          + (f", {errors} errors" if errors else ""))
    return True


# ---------------------------------------------------------------------------
# Direction: ON Notes → Lean Docstrings (--inject)
# ---------------------------------------------------------------------------

def parse_deep_analysis_content(content):
    """Extract structured fields from a Deep Analysis note's markdown."""
    result = {}
    # Title
    m = re.search(r'^## Module: (.+)$', content, re.MULTILINE)
    if m:
        result['module'] = m.group(1).strip()

    for field in ['Intent', 'Layer', 'Tags', 'Contracts', 'Cross-refs', 'Invariants']:
        m = re.search(rf'^\*\*{field}\*\*:\s*(.+)$', content, re.MULTILINE)
        if m:
            result[field.lower()] = m.group(1).strip()
    return result


def format_as_lean_block_comment(parsed):
    """Format a Deep Analysis note as a module-level /- block comment."""
    lines = ["/-"]
    if parsed.get('module'):
        lines.append(f"# Module: {parsed['module']}")
        lines.append("")

    for section in ['Intent', 'Contracts', 'Cross-refs', 'Invariants']:
        # Try lowercase key
        key = section.lower()
        val = parsed.get(key)
        if val and val != "(not set)":
            lines.append(f"## {section}")
            lines.append("")
            lines.append(val)
            lines.append("")

    if parsed.get('tags'):
        lines.append("## Tags")
        lines.append("")
        lines.append(parsed['tags'])
        lines.append("")

    lines.append("-/")
    return '\n'.join(lines)


def find_lean_file_for_module(module_name, lean_dir):
    """Find the .lean file for a module name, handling namespace prefixes."""
    # Strip namespace prefix (e.g., LaserCortex.Foo → Foo)
    if '.' in module_name:
        module_name = module_name.split('.')[-1]

    # Direct match
    path = os.path.join(lean_dir, f"{module_name}.lean")
    if os.path.exists(path):
        return path
    # Nested under LaserCortex/
    path = os.path.join(lean_dir, "LaserCortex", f"{module_name}.lean")
    if os.path.exists(path):
        return path
    # Nested under Visualization/
    path = os.path.join(lean_dir, "Visualization", f"{module_name}.lean")
    if os.path.exists(path):
        return path
    return None


def inject_block_comment(filepath, new_comment, module_name, dry_run=False):
    """Insert or update a /- block comment at the top of a .lean file.

    Strategy:
      - If a /- block comment with 'Module: <module_name>' already exists,
        update it (deduplicate).
      - Otherwise, insert the block comment after any initial -- lines
        and copyright headers, before the first import.
    """
    with open(filepath) as f:
        text = f.read()

    # Check if there's already a block comment for this module
    module_marker = f"# Module: {module_name}"
    existing_match = None
    for m in re.finditer(r'^/-\s*\n(.*?)^-/', text, re.MULTILINE | re.DOTALL):
        if module_marker in m.group(1):
            existing_match = m
            break

    if existing_match:
        old_comment = existing_match.group(0)
        if old_comment.strip() == new_comment.strip():
            return False  # No change
        new_text = text[:existing_match.start()] + new_comment + text[existing_match.end():]
        if dry_run:
            print(f"  [dry-run] WOULD UPDATE block comment in {filepath}")
            return True
        with open(filepath, 'w') as f:
            f.write(new_text)
        print(f"  Updated module doc in {filepath}")
        return True

    # No existing block comment — insert after header lines
    lines = text.split('\n')
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '' or stripped.startswith('--') or stripped.startswith('/*'):
            insert_at = i + 1
        else:
            break

    new_lines = lines[:insert_at] + [''] + new_comment.split('\n') + [''] + lines[insert_at:]

    if dry_run:
        print(f"  [dry-run] WOULD INSERT block comment in {filepath}")
        return True

    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))
    print(f"  Inserted module doc in {filepath}")
    return True


def do_inject(lean_dir, dry_run=False):
    changed = 0
    skipped = 0
    errors = 0
    injected_files = set()

    notes = find_deep_analysis_notes()
    print(f"  Found {len(notes)} Deep Analysis notes")

    for note in notes:
        title = note.get("title", "?")
        content = get_note_content(note["id"])
        if not content:
            print(f"  WARNING: Empty content for {title}")
            errors += 1
            continue

        parsed = parse_deep_analysis_content(content)
        module = parsed.get('module')
        if not module:
            print(f"  WARNING: No module name in {title}, skipping")
            skipped += 1
            continue

        filepath = find_lean_file_for_module(module, lean_dir)
        if not filepath:
            print(f"  WARNING: No .lean file found for module '{module}' ({title})")
            skipped += 1
            continue

        if filepath in injected_files:
            print(f"  Already injected into {os.path.relpath(filepath, lean_dir)}, skipping duplicate")
            continue
        injected_files.add(filepath)

        block_comment = format_as_lean_block_comment(parsed)
        if inject_block_comment(filepath, block_comment, module, dry_run=dry_run):
            changed += 1

    print(f"  Injected: {changed} files changed, {skipped} skipped"
          + (f", {errors} errors" if errors else ""))
    return True


# ---------------------------------------------------------------------------
# Check mode
# ---------------------------------------------------------------------------

def do_check(lean_dir):
    """Show what docstrings exist without writing anything."""
    print(f"Checking docstrings in {lean_dir} ...")
    total_blocks = 0
    total_decl_docs = 0
    total_module_docs = 0

    for fpath in sorted(lean_file_paths(lean_dir)):
        with open(fpath) as f:
            text = f.read()
        blocks = find_docstring_blocks(text)
        decl = [b for b in blocks if b['kind'] == 'decl_doc']
        mod = [b for b in blocks if b['kind'] == 'module_doc']
        blk = [b for b in blocks if b['kind'] == 'block_comment']
        total_blocks += len(blocks)
        total_decl_docs += len(decl)
        total_module_docs += len(mod)
        if blocks:
            rel = os.path.relpath(fpath, lean_dir)
            print(f"  {rel}: {len(decl)} decl docs, {len(mod)} module docs, {len(blk)} block comments")

    print(f"\n  Total: {total_blocks} blocks ({total_decl_docs} decl, {total_module_docs} module)")

    # Show what Deep Analysis notes exist
    da_notes = find_deep_analysis_notes()
    print(f"\n  Deep Analysis notes in ON: {len(da_notes)}")
    for n in da_notes:
        print(f"    {n.get('title', '?')}")

    # Show what would be injected
    print(f"\n  Dry-run inject: would write `/-` block comments into .lean files")
    for n in da_notes:
        content = get_note_content(n["id"])
        if content:
            parsed = parse_deep_analysis_content(content)
            module = parsed.get('module', '?')
            fp = find_lean_file_for_module(module, lean_dir)
            if fp:
                rel = os.path.relpath(fp, lean_dir)
                print(f"    {n.get('title', '?')} → {rel}")
            else:
                print(f"    {n.get('title', '?')} → [NO FILE FOUND]")

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bidirectional Lean docstring ↔ ON note sync")
    parser.add_argument("--extract", action="store_true", help="Lean docstrings → ON notes")
    parser.add_argument("--inject", action="store_true", help="ON notes → Lean docstrings")
    parser.add_argument("--check", action="store_true", help="Show status without writing")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--lean-dir", default=LASERCORTEX_DIR, help=f"Lean repo root (default: {LASERCORTEX_DIR})")
    args = parser.parse_args()

    lean_dir = os.path.abspath(args.lean_dir)
    if not os.path.isdir(lean_dir):
        print(f"ERROR: Lean directory not found: {lean_dir}")
        sys.exit(1)

    if args.check:
        do_check(lean_dir)
        return

    if not args.extract and not args.inject:
        parser.print_help()
        sys.exit(1)

    notebook_id = find_notebook_id()
    if not notebook_id:
        print(f"ERROR: ON notebook '{NOTEBOOK_NAME}' not found. Is ON running?")
        sys.exit(1)

    dry_run = args.dry_run

    if args.extract:
        ok = do_extract(notebook_id, lean_dir, dry_run=dry_run)
        if not ok:
            sys.exit(1)

    if args.inject:
        ok = do_inject(lean_dir, dry_run=dry_run)
        if not ok:
            sys.exit(1)

    if not dry_run and (args.extract or args.inject):
        print("\nDone.")


if __name__ == "__main__":
    main()
