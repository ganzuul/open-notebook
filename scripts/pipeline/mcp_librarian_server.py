#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server Bridge for Open Notebook (FastMCP edition).

Exposes ON's REST API as MCP tools via stdio, enabling Opencode
agents to create notebooks, ingest sources, run transformations, create notes,
query the semantic index, and manage the phonebook — all through ON's native
data model.

Uses the *fastmcp* framework (same as uvx lean-lsp-mcp) to keep the stdio
connection alive so Opencode does not remove the tools.

Tools:
  - query_librarian      — Semantic + text search across ON's indexed content
  - check_freshness      — Verify file integrity against SHA256 staleness key
  - get_dependency_graph — Retrieve the cross-layer dependency graph
  - list_modules         — List indexed modules filtered by layer or tag
  - create_or_get_notebook — Idempotent notebook creation by repo name
  - ingest_source        — Upload a file as a text source into a notebook
  - run_transformation   — Execute an ON transformation on text via the 35B model
  - create_note          — Create a note in a notebook (visible in ON web UI)
  - reload_librarian     — Reload the librarian phonebook index from disk
  - pipeline_status      — Check indexing pipeline health
  - search_notes         — Full-text or vector search across ON sources/notes

Architecture:
  Opencode ──MCP──► this script ──REST──► ON (localhost:5055)
                                           ├── Sources (file content)
                                           ├── Notes (semantic index entries)
                                           ├── Transformations (35B teacher)
                                           └── Search (vector + text)

  The librarian_server.py phonebook is maintained as a secondary fast-lookup
  index, refreshed via reload_librarian after regeneration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import requests
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_librarian")

ON_URL = "http://localhost:5055/api"
LIBRARIAN_URL = "http://localhost:8081"


# ── REST clients (unchanged) ──────────────────────────────────────────────


class ONClient:
    """Thin client for Open Notebook's REST API."""

    def __init__(self, base_url: str = ON_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, **kwargs) -> Any:
        r = self.session.get(self._url(path), timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, data: dict | None = None, **kwargs) -> Any:
        r = self.session.post(self._url(path), json=data, timeout=120, **kwargs)
        r.raise_for_status()
        return r.json()

    def notebooks_list(self) -> list:
        return self.get("/notebooks")

    def notebook_create(self, name: str, description: str = "") -> dict:
        return self.post("/notebooks", data={"name": name, "description": description})

    def notebook_get(self, notebook_id: str) -> dict:
        return self.get(f"/notebooks/{notebook_id}")

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
        data: dict = {
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

    def source_link_notebook(self, source_id: str, notebook_id: str) -> dict:
        return self.post(f"/notebooks/{notebook_id}/sources/{source_id}")

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
        data: dict = {"content": content, "note_type": note_type}
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
        )

    def search(
        self,
        query: str,
        search_type: str = "text",
        limit: int = 20,
        search_sources: bool = True,
        search_notes: bool = True,
        minimum_score: float = 0.2,
    ) -> dict:
        return self.post(
            "/search",
            data={
                "query": query,
                "type": search_type,
                "limit": limit,
                "search_sources": search_sources,
                "search_notes": search_notes,
                "minimum_score": minimum_score,
            },
        )

    def models_list(self) -> list:
        return self.get("/models")


class LibrarianClient:
    """Client for the librarian phonebook index (FastAPI on :8081)."""

    def __init__(self, base_url: str = LIBRARIAN_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def query(self, **kwargs) -> dict:
        r = self.session.post(f"{self.base_url}/query", json=kwargs, timeout=30)
        r.raise_for_status()
        return r.json()

    def check_freshness(self, file_path: str, expected_hash: str | None = None) -> dict:
        data: dict = {"file_path": file_path}
        if expected_hash:
            data["expected_hash"] = expected_hash
        r = self.session.post(f"{self.base_url}/check-freshness", json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_dependency_graph(self) -> dict:
        r = self.session.get(f"{self.base_url}/dependency-graph", timeout=30)
        r.raise_for_status()
        return r.json()

    def list_modules(self, layer: str | None = None, tag: str | None = None) -> dict:
        params = {}
        if layer:
            params["layer"] = layer
        if tag:
            params["tag"] = tag
        r = self.session.get(f"{self.base_url}/modules", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def reload(self) -> dict:
        r = self.session.post(f"{self.base_url}/reload", timeout=30)
        r.raise_for_status()
        return r.json()

    def pipeline_status(self) -> dict:
        r = self.session.get(f"{self.base_url}/pipeline-status", timeout=30)
        r.raise_for_status()
        return r.json()


# ── FastMCP server ────────────────────────────────────────────────────────

mcp = FastMCP(
    "open-notebook-librarian",
    version="3.0.0",
    instructions="MCP bridge to Open Notebook's semantic index, phonebook, and transformation pipeline.",
)

# Shared REST clients (module-level so tool functions can use them)
_on = ONClient()
_lib = LibrarianClient()


# ── Helper ────────────────────────────────────────────────────────────────

def _compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Tool: query_librarian ─────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Search the semantic phonebook by text, tags, or module name. "
        "Returns matching modules with their intent, tags, contracts, and cross-references."
    ),
)
def query_librarian(
    query_text: str | None = None,
    anchor_tag: str | None = None,
    related_tags: list[str] | None = None,
    edge_type: str | None = None,
    module_name: str | None = None,
) -> str:
    """
    Search the semantic phonebook by text, tags, or module name.
    Returns matching modules with their intent, tags, contracts, and cross-references.
    """
    kwargs: dict[str, Any] = {}
    if query_text is not None:
        kwargs["query_text"] = query_text
    if anchor_tag is not None:
        kwargs["anchor_tag"] = anchor_tag
    if related_tags is not None:
        kwargs["related_tags"] = related_tags
    if edge_type is not None:
        kwargs["edge_type"] = edge_type
    if module_name is not None:
        kwargs["module_name"] = module_name
    result = _lib.query(**kwargs)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: check_freshness ─────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Verify a file's integrity by comparing its SHA256 hash against the phonebook's staleness key. "
        "Run this BEFORE relying on librarian data for any file."
    ),
)
def check_freshness(
    file_path: str,
    expected_hash: str | None = None,
) -> str:
    """
    Verify a file's integrity by comparing its SHA256 hash against the phonebook's staleness key.
    """
    result = _lib.check_freshness(file_path=file_path, expected_hash=expected_hash)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: get_dependency_graph ────────────────────────────────────────────

@mcp.tool(
    description="Retrieve the full cross-layer dependency graph as JSON.",
)
def get_dependency_graph() -> str:
    """Retrieve the full cross-layer dependency graph as JSON."""
    result = _lib.get_dependency_graph()
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: list_modules ────────────────────────────────────────────────────

@mcp.tool(
    description="List all indexed modules, optionally filtered by layer or tag.",
)
def list_modules(
    layer: str | None = None,
    tag: str | None = None,
) -> str:
    """List all indexed modules, optionally filtered by layer or tag."""
    result = _lib.list_modules(layer=layer, tag=tag)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: reload_librarian ────────────────────────────────────────────────

@mcp.tool(
    description="Reload the librarian index from disk after phonebook regeneration.",
)
def reload_librarian() -> str:
    """Reload the librarian index from disk after phonebook regeneration."""
    _lib.reload()
    return json.dumps({"status": "reloaded"}, indent=2)


# ── Tool: pipeline_status ─────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Check the status of the indexing pipeline. Returns module count, edge count, "
        "phonebook directories, and indexed module names. Use this BEFORE relying on "
        "librarian data to verify the index is current and the 35B teacher pass has completed."
    ),
)
def pipeline_status() -> str:
    """Check the status of the indexing pipeline."""
    result = _lib.pipeline_status()
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: create_or_get_notebook ──────────────────────────────────────────

@mcp.tool(
    description=(
        "Create an Open Notebook notebook for a repository, or return the existing one. "
        "Use this when accessing ON for a new repo to ensure a notebook exists."
    ),
)
def create_or_get_notebook(
    name: str,
    description: str = "",
) -> str:
    """
    Create an Open Notebook notebook for a repository, or return the existing one.
    """
    notebooks = _on.notebooks_list()
    for nb in notebooks:
        if nb["name"] == name:
            return json.dumps(nb, indent=2, ensure_ascii=False)
    nb = _on.notebook_create(name=name, description=description)
    logger.info("Created notebook: %s (%s)", nb["id"], name)
    return json.dumps(nb, indent=2, ensure_ascii=False)


# ── Tool: ingest_source ───────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Ingest a file's content as a text source into an Open Notebook notebook. "
        "The source will be embedded for vector search if embed=True. "
        "Returns the source ID."
    ),
)
def ingest_source(
    notebook_id: str,
    content: str,
    title: str | None = None,
    embed: bool = True,
) -> str:
    """
    Ingest a file's content as a text source into an Open Notebook notebook.
    """
    source = _on.source_create_text(
        content=content,
        title=title,
        notebooks=[notebook_id],
        embed=embed,
        async_processing=True,
    )
    logger.info("Ingested source: %s — %s", source.get("id", "?"), title)
    return json.dumps(source, indent=2, ensure_ascii=False)


# ── Tool: run_transformation ──────────────────────────────────────────────

@mcp.tool(
    description=(
        "Execute an ON transformation on text using the teacher model. "
        "This routes through ON's LLM orchestration, ensuring the 35B model is used "
        "and the result stays within ON's data model."
    ),
)
def run_transformation(
    transformation_id: str,
    input_text: str,
    model_id: str,
) -> str:
    """
    Execute an ON transformation on text using the teacher model.
    """
    result = _on.transformation_execute(
        transformation_id=transformation_id,
        input_text=input_text,
        model_id=model_id,
    )
    logger.info("Transformation %s complete", transformation_id)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Tool: create_note ─────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Create a note in an Open Notebook notebook. Notes are visible in the ON web UI "
        "and can be used as semantic index entries visible to both humans and agents."
    ),
)
def create_note(
    notebook_id: str,
    content: str,
    title: str | None = None,
    note_type: str = "ai",
) -> str:
    """
    Create a note in an Open Notebook notebook.
    """
    note = _on.note_create(
        content=content,
        title=title,
        note_type=note_type,
        notebook_id=notebook_id,
    )
    logger.info("Created note: %s — %s", note.get("id", "?"), title)
    return json.dumps(note, indent=2, ensure_ascii=False)


# ── Tool: search_notes ────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Search across ON's sources and notes using text or vector search. "
        "Returns matching content from the semantic index."
    ),
)
def search_notes(
    query: str,
    search_type: str = "text",
    limit: int = 20,
    search_sources: bool = True,
    search_notes: bool = True,
) -> str:
    """
    Search across ON's sources and notes using text or vector search.
    """
    result = _on.search(
        query=query,
        search_type=search_type,
        limit=limit,
        search_sources=search_sources,
        search_notes=search_notes,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server over stdio using FastMCP."""
    logger.info("Starting FastMCP open-notebook-librarian server (stdio)")
    logger.info("ON API: %s", ON_URL)
    logger.info("Librarian: %s", LIBRARIAN_URL)
    # FastMCP handles the entire JSON-RPC lifecycle, including pings,
    # capability negotiation, and connection keep-alive.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
