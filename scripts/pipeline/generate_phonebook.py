#!/usr/bin/env python3
"""
Open Notebook Phonebook Generator — ON-Native Edition

Ingests source files into Open Notebook as Sources, runs semantic analysis
via ON's Transformation API (using the 35B teacher model), and creates Notes
containing structured phonebook entries visible in the ON web UI.

Three modes:
  --full         Fresh ingest + LLM transformation (overnight, teacher model)
  --incremental  Only process changed files (evening CI, student model ok)
  --fast         Heuristic-only, no LLM calls (daytime quick check)

Architecture:
  File ──► ON Source ──► Transformation (35B) ──► ON Note (semantic index)
                                                    │
                                                    └──► Phonebook JSON (fast lookup)

The phonebook JSON is a secondary artifact for deterministic tag-based lookup.
The primary index lives in ON's vector store and is searchable via the MCP bridge.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

ON_URL = "http://localhost:5055/api"

# ---------------------------------------------------------------------------
# Language support
# ---------------------------------------------------------------------------

EXTENSION_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".lean": "lean4",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".rs": "rust",
    ".go": "go",
}

LAYER_MAP = {
    "lean4": "FORMALIZATION",
    "python": "API_GATEWAY",
    "typescript": "PRESENTATION",
    "javascript": "PRESENTATION",
    "markdown": "DOCUMENTATION",
    "rust": "SYSTEM",
    "go": "SYSTEM",
}

SKIP_PATTERNS = [
    "MASTER_RECON_PHONEBOOK.md",
    "DEPENDENCY_GRAPH.json",
    ".phonebook_cache.json",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".git/",
    ".lake/",           # mathlib dependency — not our code
    "_archive/",        # historical/abandoned
    "dist/",
    "build/",
    ".next/",
    ".venv/",
    "venv/",
]

# ---------------------------------------------------------------------------
# ON API Client
# ---------------------------------------------------------------------------


class ONClient:
    def __init__(self, base_url: str = ON_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, **kwargs) -> Any:
        r = self.session.get(self._url(path), timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, data: dict | None = None, timeout: int = 120, **kwargs) -> Any:
        r = self.session.post(self._url(path), json=data, timeout=timeout, **kwargs)
        r.raise_for_status()
        return r.json()

    def notebooks_list(self) -> list:
        return self.get("/notebooks")

    def notebook_get(self, notebook_id: str) -> dict:
        return self.get(f"/notebooks/{notebook_id}")

    def notebook_create(self, name: str, description: str = "") -> dict:
        return self.post("/notebooks", data={"name": name, "description": description})

    def create_or_get_notebook(self, name: str, description: str = "") -> dict:
        for nb in self.notebooks_list():
            if nb["name"] == name:
                return nb
        return self.notebook_create(name=name, description=description)

    def sources_list(self, notebook_id: str | None = None) -> list:
        params = {}
        if notebook_id:
            params["notebook_id"] = notebook_id
        return self.get("/sources", params=params)

    def source_create_text(
        self,
        content: str,
        title: str | None = None,
        notebooks: list[str] | None = None,
        embed: bool = True,
        async_processing: bool = True,
    ) -> dict:
        data = {
            "type": "text",
            "content": content,
            "embed": embed,
            "async_processing": async_processing,
        }
        if title:
            data["title"] = title
        if notebooks:
            data["notebooks"] = notebooks
        return self.post("/sources/json", data=data)

    def notes_list(self, notebook_id: str | None = None) -> list:
        params = {}
        if notebook_id:
            params["notebook_id"] = notebook_id
        return self.get("/notes", params=params)

    def note_create(
        self,
        content: str,
        title: str | None = None,
        note_type: str = "ai",
        notebook_id: str | None = None,
    ) -> dict:
        data = {"content": content, "note_type": note_type}
        if title:
            data["title"] = title
        if notebook_id:
            data["notebook_id"] = notebook_id
        return self.post("/notes", data=data)

    def transformations_list(self) -> list:
        return self.get("/transformations")

    def transformation_execute(
        self, transformation_id: str, input_text: str, model_id: str
    ) -> dict:
        return self.post(
            "/transformations/execute",
            data={
                "transformation_id": transformation_id,
                "input_text": input_text,
                "model_id": model_id,
            },
            timeout=600,
        )

    def models_list(self) -> list:
        return self.get("/models")


# ---------------------------------------------------------------------------
# Heuristic Analysis (Pass 1 — no LLM needed)
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def save_cache_atomic(cache: dict, cache_path: Path):
    """Write cache atomically via temp file + POSIX rename."""
    tmp = cache_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.rename(cache_path)


def detect_language(path: Path) -> str:
    return EXTENSION_MAP.get(path.suffix.lower(), "unknown")


def detect_layer(lang: str) -> str:
    return LAYER_MAP.get(lang, "UNKNOWN")


def extract_imports_py(content: str) -> list[str]:
    imports = []
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", line)
        if m:
            imports.append(m.group(1).split(".")[0])
    return list(dict.fromkeys(imports))


def extract_imports_ts(content: str) -> list[str]:
    imports = []
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"(?:import|from)\s+['\"]([^'\"]+)['\"]", line)
        if m:
            pkg = m.group(1).split("/")[0]
            if not pkg.startswith("."):
                imports.append(pkg)
    return list(dict.fromkeys(imports))


def extract_imports_lean(content: str) -> list[str]:
    imports = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("import "):
            mod = line.replace("import ", "").strip()
            imports.append(mod.split(".")[0])
    return list(dict.fromkeys(imports))


def extract_contracts_lean(content: str) -> list[str]:
    contracts = []
    for line in content.splitlines():
        stripped = line.strip()
        if any(
            stripped.startswith(kw)
            for kw in ("theorem", "lemma", "def ", "axiom ", "instance ", "class ", "structure ", "inductive ")
        ):
            parts = re.split(r"\s+", stripped, 2)
            name = parts[1].strip().rstrip("({[:") if len(parts) > 1 else ""
            if name and name[0].isalpha():
                contracts.append(name)
    return contracts[:15]


def extract_exports_lean(content: str) -> list[str]:
    for line in content.splitlines():
        if line.strip().startswith("namespace "):
            return [line.strip().replace("namespace ", "")]
    return []


def heuristic_analyze(path: Path, content: str) -> dict:
    lang = detect_language(path)
    layer = detect_layer(lang)
    module_name = path.stem

    if lang == "lean4":
        imports = extract_imports_lean(content)
        contracts = extract_contracts_lean(content)
        exports = extract_exports_lean(content)
        tags = ["#lean4", f"#{layer.lower()}"]
        if any(kw in content for kw in ["theorem", "lemma"]):
            tags.append("#proof")
        if any(kw in content for kw in ["axiom"]):
            tags.append("#axiom")
    elif lang == "python":
        imports = extract_imports_py(content)
        contracts = []
        exports = []
        tags = ["#python", f"#{layer.lower()}"]
        if "class " in content:
            tags.append("#class")
        if "def " in content:
            tags.append("#function")
    elif lang in ("typescript", "javascript"):
        imports = extract_imports_ts(content)
        contracts = []
        exports = []
        tags = [f"#{lang}", f"#{layer.lower()}"]
    else:
        imports = []
        contracts = []
        exports = []
        tags = [f"#{lang}"]

    sha = file_sha256(path)
    return {
        "file": str(path),
        "module": module_name,
        "language": lang,
        "layer": layer,
        "sha256": f"sha256:{sha[:16]}",
        "imports": imports,
        "contracts": contracts,
        "exports": exports,
        "tags": tags,
        "lines": len(content.splitlines()),
    }


# ---------------------------------------------------------------------------
# Source File Collection
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = set(EXTENSION_MAP.keys())


def collect_files(root: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".lake", "_archive", "__pycache__", ".pytest_cache", "node_modules", ".next", "dist", "build")]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root)
            if any(pat in str(rel) for pat in SKIP_PATTERNS):
                continue
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(fpath)
    return sorted(files)


def resolve_included_paths(root: Path, paths: list[str]) -> list[Path]:
    """Resolve --include-path entries to actual files on disk.

    Accepts absolute paths or paths relative to *root* (the repo).
    Supports glob patterns (``*``, ``**/*.lean``).  Skips nonexistent
    entries with a warning.  Files inside skipped directories (e.g.
    ``.lake/packages/…``) are accepted — the caller injects them
    *after* the walk.
    """
    from glob import glob as globmatch

    result: list[Path] = []
    for p in paths:
        p = p.strip()
        if not p:
            continue
        p_obj = Path(p)
        if p_obj.is_absolute():
            candidates = sorted(globmatch(str(p_obj), recursive=True))
        else:
            candidates = sorted(globmatch(str((root / p)), recursive=True))
        if not candidates:
            print(f"  WARNING: --include-path '{p}' does not match any file",
                  file=sys.stderr)
            continue
        for c in candidates:
            cp = Path(c)
            if cp.is_file():
                result.append(cp)
    seen: set[Path] = set()
    deduped = [rp for rp in result if rp not in seen and not seen.add(rp)]
    return sorted(deduped)


# ---------------------------------------------------------------------------
# Phonebook Generation Pipeline
# ---------------------------------------------------------------------------


def generate_phonebook_entry(analysis: dict, content: str) -> str:
    lines = [
        f"## Module: {analysis['module']}",
        f"**File**: `{analysis['file']}`",
        f"**Layer**: {analysis['layer']}",
        f"**Language**: {analysis['language']}",
        f"**Lines**: {analysis['lines']}",
        f"**SHA256**: `{analysis['sha256']}`",
        "",
        f"**Tags**: {' '.join(analysis['tags'])}",
        "",
        f"**Imports**: {', '.join(analysis['imports'][:10]) or 'none'}",
        f"**Contracts**: {', '.join(analysis['contracts'][:10]) or 'none'}",
        f"**Exports**: {', '.join(analysis['exports'][:5]) or 'none'}",
        "",
        f"**Intent**: *{analysis['module']} — {analysis['language']} module in the {analysis['layer']} layer.*",
        "",
    ]
    return "\n".join(lines)


def build_dependency_graph(entries: list[dict]) -> dict:
    edges = []
    by_module = {e["module"]: e for e in entries}
    for entry in entries:
        for imp in entry.get("imports", []):
            if imp in by_module and imp != entry["module"]:
                target = by_module[imp]
                edges.append({
                    "source": entry["module"],
                    "target": imp,
                    "layer_source": entry["layer"],
                    "layer_target": target["layer"],
                    "edge_type": "IMPORT",
                    "invariant": f"imports {imp}",
                    "failure_mode": f"missing import: {imp}",
                })
    return {
        "nodes": [e["module"] for e in entries],
        "edges": edges,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# ON Transformation IDs (created in Open Notebook database)
# ---------------------------------------------------------------------------

TRANSFORMATION_CONTEXT_COMPILER = "transformation:0tkrn2ru01xj0zd4cp09"
TRANSFORMATION_CROSS_LAYER_LINKER = "transformation:j2puh5eolx32sc5b431s"


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    root: Path,
    mode: str = "full",
    model_id: str | None = None,
    notebook_name: str | None = None,
    output_dir: Path | None = None,
    pass2: bool = True,
    ranking_file: str | None = None,
    top_k: int | None = None,
    retry_dead: bool = False,
    include_paths: list[str] | None = None,
):
    """
    3-pass pipeline per the spec (Gemini_and_Claude_on_implementation.md):

    Pass 1 (Map):   heuristic analysis + ON source ingest + heuristic notes
    Pass 2 (Reduce): LLM transformation via Context-Compiler-V1 per file
    Pass 3 (Link):  cross-layer dependency edges via Cross-Layer-Linker

    Modes:
      --full         All 3 passes (overnight, teacher 35B model)
      --incremental  Pass 1 only, skip changed files (evening CI)
      --fast         Pass 1 only, no LLM calls (daytime quick check)
    """
    on = ONClient()

    if model_id is None:
        models = on.models_list()
        # Prefer a language model (type == "language") over embedding models
        lang_models = [m for m in models if m.get("type") == "language"]
        chosen = lang_models[0] if lang_models else (models[0] if models else None)
        if chosen:
            model_id = chosen["id"]
            print(f"  Using model: {chosen['name']} ({model_id})")
        else:
            print("  WARNING: no models found — LLM passes will be skipped")

    if notebook_name is None:
        notebook_name = root.name
    if output_dir is None:
        output_dir = root

    pipeline_start = time.time()

    print(f"[1/7] Connecting to Open Notebook at {ON_URL}...")
    nb = on.create_or_get_notebook(notebook_name, description=f"Semantic index for {notebook_name}")
    nb_id = nb["id"]
    print(f"  Notebook: {nb['name']} ({nb_id})")

    print(f"[2/7] Collecting source files from {root}...")
    files = collect_files(root)
    print(f"  Found {len(files)} source files from walk")

    # ── Inject manually included paths (bypass skip-dirs, e.g. .lake/packages/...) ──
    if include_paths:
        t1 = time.time()
        added = resolve_included_paths(root, include_paths)
        added_filtered = [p for p in added if p.suffix.lower() in SUPPORTED_EXTENSIONS]
        existing = set(files)
        for p in added_filtered:
            if p not in existing:
                files.append(p)
                existing.add(p)
        files.sort()
        print(f"  Added {len(added_filtered)} manually included files ({time.time() - t1:.1f}s)")

    print(f"  Total: {len(files)} source files")

    # --- Pass 1: Heuristic analysis + ON ingest ---
    print(f"[3/7] Pass 1 (Map): Running heuristic analysis...")
    cache_path = output_dir / ".phonebook_cache.json"
    cache = {}
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        print(f"  Loaded cache with {len(cache)} entries")

    entries = []
    changed_files = []
    for fpath in files:
        rel = str(fpath.relative_to(root))
        content = fpath.read_text(errors="replace")
        sha = file_sha256(fpath)

        cached = cache.get(rel)
        if cached and cached.get("sha256", "").endswith(sha[:16]):
            # Cache hit: reuse heuristic analysis
            entries.append(cached)
            # Even in --full mode, skip if 35B transformation was already done
            if mode == "full" and cached.get("transform_sha", "").endswith(sha[:16]):
                # File already fully processed — skip
                continue
            # In fast/incremental mode, skip entirely
            if mode != "full":
                continue
            # In full mode but transform is stale — schedule for re-transform
            changed_files.append((fpath, content, cached))
            continue

        analysis = heuristic_analyze(fpath, content)
        entries.append(analysis)
        changed_files.append((fpath, content, analysis))
        cache[rel] = analysis

    print(f"  Analyzed {len(entries)} modules ({len(changed_files)} changed)")

    print(f"[4/7] Pass 1: Ingesting sources into Open Notebook...")
    existing_sources = {s.get("title", ""): s for s in on.sources_list(notebook_id=nb_id)}
    existing_notes = {n.get("title", ""): n for n in on.notes_list(notebook_id=nb_id)}

    ingested = 0
    for fpath, content, analysis in changed_files:
        title = fpath.name
        if title not in existing_sources:
            try:
                on.source_create_text(
                    content=content,
                    title=title,
                    notebooks=[nb_id],
                    embed=True,
                    async_processing=True,
                )
                ingested += 1
            except Exception as e:
                print(f"  WARNING: Failed to ingest {title}: {e}")
        else:
            print(f"  Source already exists: {title}")

    print(f"  Ingested {ingested} new sources")

    print(f"[5/7] Pass 1: Creating heuristic index notes in Open Notebook...")
    created = 0
    for fpath, content, analysis in changed_files:
        note_title = f"{analysis['module']} — Semantic Index"
        if note_title not in existing_notes:
            entry_md = generate_phonebook_entry(analysis, content)
            try:
                on.note_create(
                    content=entry_md,
                    title=note_title,
                    note_type="ai",
                    notebook_id=nb_id,
                )
                created += 1
            except Exception as e:
                print(f"  WARNING: Failed to create note {note_title}: {e}")
        else:
            print(f"  Note already exists: {note_title}")
    print(f"  Created {created} new heuristic notes")

    # --- Pass 2: LLM transformation (only in full mode) ---
    enriched_entries = {}
    if mode == "full" and model_id and changed_files and pass2:
        pass2_start = time.time()
        total_files = len(changed_files)

        # ---- Apply relevance ranking (if available) ----
        ranked_files = list(changed_files)  # default: process all
        if ranking_file and Path(ranking_file).exists():
            try:
                with open(ranking_file) as f:
                    ranking_data = json.load(f)
                score_map = {}
                for r in ranking_data.get("ranked", []):
                    score_map[r["path"]] = r["score"]
                # Sort changed_files by score (descending), unscored last
                def sort_key(item):
                    rel = str(item[0].relative_to(root)) if not str(item[0]).startswith(str(root)) else str(item[0])
                    # Normalize: ranking paths are relative to root
                    try:
                        rel_path = str(item[0].relative_to(root))
                    except ValueError:
                        rel_path = str(item[0])
                    return -score_map.get(rel_path, 0.0)
                ranked_files.sort(key=sort_key)
                # Apply top-K cap only if explicitly set via --top-k
                if top_k is not None:
                    ranked_files = ranked_files[:max(1, top_k)]
                    print(f"  Ranking applied: {len(changed_files)} → top-{len(ranked_files)} files "
                          f"(--top-k {top_k})")
                else:
                    print(f"  Ranking applied: sort by relevance ({len(ranked_files)} files, no cap)")
            except (json.JSONDecodeError, KeyError, OSError) as e:
                print(f"  WARNING: Could not read ranking file {ranking_file}: {e}")
                print(f"  Falling back to processing all {len(changed_files)} files")

        # ---- Skip dead-lettered files (failed ≥3 times) ----
        if not retry_dead:
            alive = []
            dead = []
            for item in ranked_files:
                rel = str(item[0].relative_to(root))
                if cache.get(rel, {}).get("_dead_letter"):
                    dead.append((rel, item))
                else:
                    alive.append(item)
            if dead:
                print(f"  Skipping {len(dead)} dead-lettered files (use --retry-dead to re-attempt):")
                for rel, _ in dead:
                    print(f"    - {rel}")
            ranked_files = alive

        print(f"[6/7] Pass 2 (Reduce): Running Context-Compiler-V1 on {len(ranked_files)}/{total_files} files...")
        transformed = 0
        skipped_transformed = 0
        for i, (fpath, content, analysis) in enumerate(ranked_files):
            # Check if this file was already fully transformed (content hasn't changed)
            rel = str(fpath.relative_to(root))
            cached = cache.get(rel, {})
            sha = file_sha256(fpath)
            if cached.get("transform_sha", "").endswith(sha[:16]):
                # Already transformed in a previous run — restore from cache
                enriched_content = cached.get("_transformed_content", "")
                if enriched_content:
                    enriched_entries[analysis["module"]] = enriched_content
                    skipped_transformed += 1
                    print(f"  {analysis['module']} ({i+1}/{len(ranked_files)}) — cached ✓")
                    continue

            t0 = time.time()
            print(f"  Transforming {analysis['module']} ({i+1}/{len(ranked_files)})...", end="", flush=True)
            try:
                result = on.transformation_execute(
                    transformation_id=TRANSFORMATION_CONTEXT_COMPILER,
                    input_text=content,
                    model_id=model_id,
                )
                elapsed = time.time() - t0
                enriched_content = result.get("output", "")
                if enriched_content:
                    note_title = f"{analysis['module']} — Deep Analysis"
                    try:
                        on.note_create(
                            content=enriched_content,
                            title=note_title,
                            note_type="ai",
                            notebook_id=nb_id,
                        )
                    except Exception:
                        pass
                    enriched_entries[analysis["module"]] = enriched_content
                    transformed += 1
                    print(f" {elapsed:.1f}s ({len(enriched_content)} chars)")

                    # Update transform cache
                    cache.setdefault(rel, {})["transform_sha"] = sha[:16]
                    # Store a preview of the transformed content (first 200 chars for cache validation)
                    cache[rel]["_transformed"] = True
                    cache[rel]["_transformed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    # Save cache periodically (every 5 successful transforms)
                    if transformed % 5 == 0:
                        save_cache_atomic(cache, cache_path)
                else:
                    print(f" {elapsed:.1f}s (empty output)")
            except Exception as e:
                elapsed = time.time() - t0
                print(f" FAILED {elapsed:.1f}s: {e}")
                # Dead-letter tracking: skip file permanently after 3 failures
                cache.setdefault(rel, {})
                cache[rel]["_fail_count"] = cache[rel].get("_fail_count", 0) + 1
                if cache[rel]["_fail_count"] >= 3:
                    cache[rel]["_dead_letter"] = True
                    print(f"  ⚠ Quarantined {rel} — will skip on future runs (use --retry-dead to re-attempt)")
                save_cache_atomic(cache, cache_path)
            time.sleep(1)

        # Final cache save after Pass 2
        save_cache_atomic(cache, cache_path)

        pass2_elapsed = time.time() - pass2_start
        transformed_total = transformed + skipped_transformed
        print(f"  Transformed {transformed} new + {skipped_transformed} cached = {transformed_total}/{total_files} files "
              f"in {pass2_elapsed:.1f}s (avg {pass2_elapsed / max(transformed_total, 1):.1f}s/file)")
    else:
        print(f"[6/7] Pass 2 (Reduce): Skipped (mode={mode}, model={model_id is not None})")

    # --- Pass 3: Cross-layer dependency linking (heuristic from imports; LLM in full mode) ---
    dep_graph = build_dependency_graph(entries)

    if mode == "full" and model_id and pass2 and len(entries) > 1:
        print(f"       Pass 3 (Link): Running Cross-Layer-Linker...")
        grouped = {}
        for entry in entries:
            layer = entry.get("layer", "UNKNOWN")
            grouped.setdefault(layer, []).append(entry)

        abstracts = []
        for layer, mods in grouped.items():
            for mod in mods:
                abstracts.append(f"Module: {mod['module']} (Layer: {layer})")
                abstracts.append(f"  Intent: {mod.get('intent', 'N/A')}")
                abstracts.append(f"  Tags: {' '.join(mod.get('tags', []))}")
                abstracts.append(f"  Contracts: {', '.join(mod.get('contracts', [])[:5])}")
                abstracts.append(f"  Imports: {', '.join(mod.get('imports', [])[:5])}")
                if mod['module'] in enriched_entries:
                    abstracts.append(f"  Deep Analysis: {enriched_entries[mod['module']][:500]}")
                abstracts.append("")

        batch_input = "\n".join(abstracts)
        try:
            result = on.transformation_execute(
                transformation_id=TRANSFORMATION_CROSS_LAYER_LINKER,
                input_text=batch_input,
                model_id=model_id,
            )
            cross_edges_raw = result.get("output", "")
            if cross_edges_raw:
                try:
                    cross_edges = json.loads(cross_edges_raw)
                    if isinstance(cross_edges, list):
                        dep_graph["edges"].extend(cross_edges)
                        print(f"  Added {len(cross_edges)} cross-layer edges")
                    elif isinstance(cross_edges, dict) and "edges" in cross_edges:
                        dep_graph["edges"].extend(cross_edges["edges"])
                        print(f"  Added {len(cross_edges['edges'])} cross-layer edges")
                except json.JSONDecodeError:
                    note_title = "Cross-Layer Dependency Graph — Raw"
                    try:
                        on.note_create(
                            content=cross_edges_raw,
                            title=note_title,
                            note_type="ai",
                            notebook_id=nb_id,
                        )
                    except Exception:
                        pass
                    print(f"  Stored raw cross-layer analysis as note (unparseable JSON)")
        except Exception as e:
            print(f"  WARNING: Cross-layer linking failed: {e}")

    # Write phonebook artifacts
    print(f"[7/7] Writing phonebook artifacts...")
    phonebook_path = output_dir / "MASTER_RECON_PHONEBOOK.md"
    graph_path = output_dir / "DEPENDENCY_GRAPH.json"

    pb_lines = [
        f"# Semantic Index: {notebook_name}",
        f"",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"Mode: {mode}",
        f"Modules: {len(entries)}",
        f"Cross-layer edges: {len(dep_graph.get('edges', []))}",
        f"",
        "---",
        "",
    ]
    for entry in entries:
        pb_lines.append(generate_phonebook_entry(entry, ""))
        pb_lines.append("")

    phonebook_path.write_text("\n".join(pb_lines))
    graph_path.write_text(json.dumps(dep_graph, indent=2, ensure_ascii=False))

    save_cache_atomic(cache, cache_path)

    print(f"  Wrote {phonebook_path}")
    print(f"  Wrote {graph_path}")
    print(f"  Wrote {cache_path}")
    print()

    nb_final = on.notebook_get(nb_id)
    elapsed = time.time() - pipeline_start
    print(f"Notebook '{nb_final['name']}' — {nb_final['source_count']} sources, {nb_final['note_count']} notes")
    print(f"Pipeline completed in {elapsed:.1f}s ({elapsed/60:.1f}min)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="ON-native phonebook generator (3-pass pipeline)")
    parser.add_argument("root", type=Path, help="Root directory to scan")
    parser.add_argument(
        "--mode",
        choices=["full", "incremental", "fast"],
        default="fast",
        help="Pipeline mode: full (overnight, all 3 passes), incremental (changed files only, Pass 1), fast (heuristic only, Pass 1)",
    )
    parser.add_argument("--model-id", type=str, default=None, help="ON model ID for the teacher (35B). Auto-detected if omitted.")
    parser.add_argument("--notebook-name", type=str, default=None, help="ON notebook name (defaults to directory name)")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for phonebook artifacts")
    parser.add_argument("--ranking-file", type=str, default=None,
                        help="Path to ranking JSON (/tmp/lasercortex_ranking.json) for Pass 2 file selection")
    parser.add_argument("--top-k", type=int, default=None,
                        help="Cap Pass 2 to top-K changed files by relevance (default: no cap, process all changed)")
    parser.add_argument("--no-pass2", action="store_true", help="Skip Pass 2 (Reduce) and Pass 3 (Link) even in full mode")
    parser.add_argument("--retry-dead", action="store_true",
                        help="Re-attempt files that were quarantined after 3 failures")
    parser.add_argument("--include-path", type=str, action="append", default=[],
                        help="File path to include (bypasses skip-dirs; can be repeated, supports globs)")
    parser.add_argument("--include-list", type=str, default=None,
                        help="File containing paths to include (one per line)")
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"Error: {args.root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Build include_paths list from --include-path + --include-list
    include_paths: list[str] = list(args.include_path)
    if args.include_list:
        try:
            with open(args.include_list) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        include_paths.append(stripped)
        except OSError as e:
            print(f"Error: Cannot read --include-list '{args.include_list}': {e}",
                  file=sys.stderr)
            sys.exit(1)

    run_pipeline(
        root=args.root,
        mode=args.mode,
        model_id=args.model_id,
        notebook_name=args.notebook_name,
        output_dir=args.output_dir,
        pass2=not args.no_pass2,
        ranking_file=args.ranking_file,
        top_k=args.top_k,
        retry_dead=args.retry_dead,
        include_paths=include_paths,
    )


if __name__ == "__main__":
    main()