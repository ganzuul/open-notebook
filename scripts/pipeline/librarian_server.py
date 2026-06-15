#!/usr/bin/env python3
"""
Librarian Index Server — ON-Native Edition

FastAPI server that provides deterministic tag/layer/module lookup against
the phonebook JSON. This is a fast secondary index — the primary semantic
search goes through ON's vector store via the MCP bridge.

Endpoints:
  GET  /modules          — List modules (filter by layer, tag)
  POST /query            — Search by text/tag/module_name
  POST /check-freshness  — SHA256 staleness check
  GET  /dependency-graph — Full dependency graph
  POST /reload           — Reload phonebook from disk
  POST /sync-from-on     — Rebuild phonebook JSON from ON's notes
"""

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ON_URL = "http://localhost:5055/api"

app = FastAPI(title="Librarian Index", version="2.0.0")

phonebook_modules: list[dict] = []
dependency_graph: dict = {"nodes": [], "edges": []}
project_root: Path | None = None
search_roots: list[Path] = []
phonebook_dirs: list[Path] = []


class QueryRequest(BaseModel):
    query_text: str | None = None
    anchor_tag: str | None = None
    related_tags: list[str] | None = None
    edge_type: str | None = None
    module_name: str | None = None


class FreshnessRequest(BaseModel):
    file_path: str
    expected_hash: str | None = None


def _parse_phonebook(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text()
    modules = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("## Module: "):
            if current:
                modules.append(current)
            current = {
                "module": line.replace("## Module: ", "").strip(),
                "layer": "",
                "language": "",
                "tags": [],
                "imports": [],
                "contracts": [],
                "exports": [],
                "sha256": "",
                "file": "",
                "lines": 0,
                "intent": "",
            }
        elif current:
            stripped = line.strip()
            if stripped.startswith("**Layer**:"):
                current["layer"] = stripped.replace("**Layer**:", "").strip()
            elif stripped.startswith("**Language**:"):
                current["language"] = stripped.replace("**Language**:", "").strip()
            elif stripped.startswith("**Tags**:"):
                tag_str = stripped.replace("**Tags**:", "").strip()
                current["tags"] = re.findall(r"#\w+", tag_str)
            elif stripped.startswith("**Imports**:"):
                imp_str = stripped.replace("**Imports**:", "").strip().rstrip(".")
                current["imports"] = [i.strip() for i in imp_str.split(",") if i.strip() and i.strip() != "none"]
            elif stripped.startswith("**Contracts**:"):
                c_str = stripped.replace("**Contracts**:", "").strip().rstrip(".")
                current["contracts"] = [c.strip() for c in c_str.split(",") if c.strip() and c.strip() != "none"]
            elif stripped.startswith("**Exports**:"):
                e_str = stripped.replace("**Exports**:", "").strip().rstrip(".")
                current["exports"] = [e.strip() for e in e_str.split(",") if e.strip() and e.strip() != "none"]
            elif stripped.startswith("**SHA256**:"):
                sha = stripped.replace("**SHA256**:", "").strip().strip("`")
                current["sha256"] = sha
            elif stripped.startswith("**File**:"):
                current["file"] = stripped.replace("**File**:", "").strip().strip("`")
            elif stripped.startswith("**Lines**:"):
                try:
                    current["lines"] = int(stripped.replace("**Lines**:", "").strip())
                except ValueError:
                    pass
            elif stripped.startswith("**Intent**:"):
                current["intent"] = stripped.replace("**Intent**:", "").strip().strip("*")
    if current:
        modules.append(current)
    return modules


def load_phonebook():
    global phonebook_modules, dependency_graph, project_root, search_roots, phonebook_dirs
    phonebook_modules.clear()

    search_roots = [
        Path("/home/nos/labware/open-notebook"),
        Path("/home/nos/labware/LaserCortex"),
    ]
    phonebook_dirs = []
    for root in search_roots:
        for pb in root.rglob("MASTER_RECON_PHONEBOOK.md"):
            phonebook_modules.extend(_parse_phonebook(pb))
            phonebook_dirs.append(pb.parent)
            project_root = pb.parent

        for dg in root.rglob("DEPENDENCY_GRAPH.json"):
            with open(dg) as f:
                data = json.load(f)
                dependency_graph["nodes"].extend(data.get("nodes", []))
                dependency_graph["edges"].extend(data.get("edges", []))

    print(f"Loaded {len(phonebook_modules)} modules, {len(dependency_graph['edges'])} edges")


@app.on_event("startup")
def startup():
    load_phonebook()


@app.get("/modules")
def list_modules(layer: str | None = None, tag: str | None = None):
    results = phonebook_modules
    if layer:
        results = [m for m in results if m.get("layer") == layer]
    if tag:
        results = [m for m in results if tag in m.get("tags", [])]
    return results


@app.post("/query")
def query(req: QueryRequest):
    results = phonebook_modules
    if req.module_name:
        results = [m for m in results if req.module_name.lower() in m.get("module", "").lower()]
    if req.anchor_tag:
        results = [m for m in results if req.anchor_tag in m.get("tags", [])]
    if req.related_tags:
        for t in req.related_tags:
            results = [m for m in results if t in m.get("tags", [])]
    if req.query_text:
        ql = req.query_text.lower()
        results = [
            m
            for m in results
            if ql in m.get("intent", "").lower()
            or ql in m.get("module", "").lower()
            or ql in " ".join(m.get("tags", [])).lower()
            or ql in " ".join(m.get("contracts", [])).lower()
        ]
    if req.edge_type:
        relevant = {
            e["source"] for e in dependency_graph.get("edges", []) if e.get("edge_type") == req.edge_type
        }
        results = [m for m in results if m.get("module") in relevant]
    return {"results": results, "total_count": len(results)}


@app.post("/check-freshness")
def check_freshness(req: FreshnessRequest):
    fpath = Path(req.file_path)
    if not fpath.is_absolute():
        resolved = None
        for d in phonebook_dirs:
            candidate = d / req.file_path
            if candidate.exists():
                resolved = candidate
                break
        if not resolved:
            return {"file_path": req.file_path, "status": "missing", "reason": "file not found in any phonebook root"}
        fpath = resolved
    elif not fpath.exists():
        return {"file_path": req.file_path, "status": "missing", "reason": "file not found"}
    sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
    current = f"sha256:{sha[:16]}"

    stored = None
    for m in phonebook_modules:
        if m.get("file", "").endswith(req.file_path) or req.file_path in m.get("file", ""):
            stored = m.get("sha256")
            break

    if req.expected_hash:
        stored = req.expected_hash

    if stored and stored == current:
        return {"file_path": req.file_path, "status": "fresh", "current_hash": current, "stored_hash": stored}
    elif stored:
        return {"file_path": req.file_path, "status": "stale", "current_hash": current, "stored_hash": stored}
    else:
        return {"file_path": req.file_path, "status": "unknown", "current_hash": current, "stored_hash": None}


@app.get("/dependency-graph")
def get_dependency_graph():
    return dependency_graph


@app.get("/pipeline-status")
def pipeline_status():
    heuristic_only = [m["module"] for m in phonebook_modules if m.get("sha256")]
    return {
        "modules_indexed": len(phonebook_modules),
        "edges_indexed": len(dependency_graph["edges"]),
        "phonebook_dirs": [str(d) for d in phonebook_dirs],
        "modules": heuristic_only,
    }


@app.post("/reload")
def reload():
    load_phonebook()
    return {"status": "reloaded", "modules": len(phonebook_modules), "edges": len(dependency_graph["edges"])}


@app.post("/sync-from-on")
def sync_from_on(notebook_name: str | None = None):
    """Rebuild phonebook JSON from ON's notes — for when the phonebook file is stale."""
    try:
        on = requests.Session()
        notebooks = on.get(f"{ON_URL}/notebooks", timeout=30).json()
        target_nb = None
        for nb in notebooks:
            if notebook_name and nb["name"] == notebook_name:
                target_nb = nb
                break
            elif not notebook_name:
                target_nb = nb

        if not target_nb:
            return {"status": "error", "reason": f"Notebook '{notebook_name}' not found"}

        notes = on.get(f"{ON_URL}/notes", params={"notebook_id": target_nb["id"]}, timeout=30).json()
        sources = on.get(f"{ON_URL}/sources", params={"notebook_id": target_nb["id"]}, timeout=30).json()

        modules = []
        for note in notes:
            title = note.get("title") or ""
            content = note.get("content") or ""
            if "Semantic Index" in title or "Deep Analysis" in title:
                module_name = title.split(" — ")[0].strip() if " — " in title else title
                tags = re.findall(r"#\w+", content)
                layer = ""
                for line in content.splitlines():
                    if "**Layer**:" in line:
                        layer = line.split("**Layer**:")[1].strip()
                modules.append({
                    "module": module_name,
                    "title": title,
                    "tags": tags,
                    "layer": layer,
                    "content_preview": content[:200],
                    "note_id": note["id"],
                })

        return {
            "status": "synced",
            "notebook": target_nb["name"],
            "sources": len(sources),
            "notes": len(notes),
            "indexed_modules": len(modules),
            "modules": modules,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)