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
    ".git/",
    "dist/",
    "build/",
    ".next/",
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
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules", ".next", "dist", "build")]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root)
            if any(pat in str(rel) for pat in SKIP_PATTERNS):
                continue
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(fpath)
    return sorted(files)


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
        if models:
            model_id = models[0]["id"]
            print(f"  Using model: {models[0]['name']} ({model_id})")

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
    print(f"  Found {len(files)} source files")

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
        if cached and cached.get("sha256", "").endswith(sha[:16]) and mode != "full":
            entries.append(cached)
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
        print(f"[6/7] Pass 2 (Reduce): Running Context-Compiler-V1 on {len(changed_files)} files...")
        transformed = 0
        for i, (fpath, content, analysis) in enumerate(changed_files):
            t0 = time.time()
            print(f"  Transforming {analysis['module']} ({i+1}/{len(changed_files)})...", end="", flush=True)
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
                else:
                    print(f" {elapsed:.1f}s (empty output)")
            except Exception as e:
                elapsed = time.time() - t0
                print(f" FAILED {elapsed:.1f}s: {e}")
            time.sleep(1)
        pass2_elapsed = time.time() - pass2_start
        print(f"  Transformed {transformed}/{len(changed_files)} files in {pass2_elapsed:.1f}s (avg {pass2_elapsed/max(len(changed_files),1):.1f}s/file)")
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

    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)

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
    parser.add_argument("--no-pass2", action="store_true", help="Skip Pass 2 (Reduce) and Pass 3 (Link) even in full mode")
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"Error: {args.root} is not a directory", file=sys.stderr)
        sys.exit(1)

    run_pipeline(
        root=args.root,
        mode=args.mode,
        model_id=args.model_id,
        notebook_name=args.notebook_name,
        output_dir=args.output_dir,
        pass2=not args.no_pass2,
    )


if __name__ == "__main__":
    main()