#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server Bridge for Open Notebook.

Exposes ON's REST API as MCP tools via stdio JSON-RPC, enabling Opencode
agents to create notebooks, ingest sources, run transformations, create notes,
query the semantic index, and manage the phonebook — all through ON's native
data model.

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

Architecture:
  Opencode ──MCP──► this script ──REST──► ON (localhost:5055)
                                           ├── Sources (file content)
                                           ├── Notes (semantic index entries)
                                           ├── Transformations (35B teacher)
                                           └── Search (vector + text)

  The librarian_server.py phonebook is maintained as a secondary fast-lookup
  index, refreshed via reload_librarian after regeneration.
"""

import json
import sys
import hashlib
import logging
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_librarian")

ON_URL = "http://localhost:5055/api"
LIBRARIAN_URL = "http://localhost:8081"


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


class MCPServer:
    def __init__(self):
        self.on = ONClient()
        self.lib = LibrarianClient()
        self._next_id = 1

    def send(self, msg: dict):
        line = json.dumps(msg, ensure_ascii=False)
        print(line, flush=True)
        logger.debug(f"<- {line[:200]}")

    def recv(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        logger.debug(f"-> {line[:200]}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            return None

    def run(self):
        logger.info("MCP Librarian Server started (ON-backed)")
        logger.info(f"ON API: {ON_URL}")
        logger.info(f"Librarian: {LIBRARIAN_URL}")
        while True:
            msg = self.recv()
            if msg is None:
                logger.info("EOF received, shutting down")
                break
            msg_id = msg.get("id")
            method = msg.get("method", "")
            params = msg.get("params", {})
            if method == "initialize":
                self._handle_initialize(msg_id, params)
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                self._handle_tools_list(msg_id)
            elif method == "tools/call":
                self._handle_tools_call(msg_id, params)
            elif method == "ping":
                self.send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            else:
                self.send_error(msg_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, msg_id, params):
        self.send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "open-notebook-librarian",
                    "version": "2.0.0",
                },
            },
        })

    def _handle_tools_list(self, msg_id):
        tools = [
            {
                "name": "query_librarian",
                "description": (
                    "Search the semantic phonebook by text, tags, or module name. "
                    "Returns matching modules with their intent, tags, contracts, and cross-references."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query_text": {
                            "type": "string",
                            "description": "Free-text search (e.g., 'podcast generation', 'Tamari lattice')",
                        },
                        "anchor_tag": {
                            "type": "string",
                            "description": "Primary tag to filter by (e.g., '#lean4', '#paradox')",
                        },
                        "related_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional tags to require",
                        },
                        "edge_type": {
                            "type": "string",
                            "description": "Edge type for dependency graph queries (e.g., 'IMPORT', 'DATA_SOURCE')",
                        },
                        "module_name": {
                            "type": "string",
                            "description": "Exact or partial module name to look up",
                        },
                    },
                },
            },
            {
                "name": "check_freshness",
                "description": (
                    "Verify a file's integrity by comparing its SHA256 hash against the phonebook's staleness key."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file (e.g., 'LaserCortex/BasicLogic.lean')",
                        },
                        "expected_hash": {
                            "type": "string",
                            "description": "Optional SHA256 prefix to compare against",
                        },
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "get_dependency_graph",
                "description": "Retrieve the full cross-layer dependency graph as JSON.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_modules",
                "description": "List all indexed modules, optionally filtered by layer or tag.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "description": "Filter by architectural layer (e.g., 'FORMALIZATION')",
                        },
                        "tag": {
                            "type": "string",
                            "description": "Filter by tag (e.g., '#lean4')",
                        },
                    },
                },
            },
            {
                "name": "reload_librarian",
                "description": "Reload the librarian index from disk after phonebook regeneration.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "pipeline_status",
                "description": (
                    "Check the status of the indexing pipeline. Returns module count, edge count, "
                    "phonebook directories, and indexed module names. Use this BEFORE relying on "
                    "librarian data to verify the index is current and the 35B teacher pass has completed."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "create_or_get_notebook",
                "description": (
                    "Create an Open Notebook notebook for a repository, or return the existing one. "
                    "Use this when accessing ON for a new repo to ensure a notebook exists."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Notebook name (typically the repo name, e.g. 'LaserCortex')",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional description for the notebook",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "ingest_source",
                "description": (
                    "Ingest a file's content as a text source into an Open Notebook notebook. "
                    "The source will be embedded for vector search if embed=True. "
                    "Returns the source ID."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "notebook_id": {
                            "type": "string",
                            "description": "The notebook ID to add the source to",
                        },
                        "title": {
                            "type": "string",
                            "description": "Title for the source (typically the filename)",
                        },
                        "content": {
                            "type": "string",
                            "description": "The text content of the source file",
                        },
                        "embed": {
                            "type": "boolean",
                            "description": "Whether to vectorize the content for semantic search (default: true)",
                        },
                    },
                    "required": ["notebook_id", "content"],
                },
            },
            {
                "name": "run_transformation",
                "description": (
                    "Execute an ON transformation on text using the teacher model. "
                    "This routes through ON's LLM orchestration, ensuring the 35B model is used "
                    "and the result stays within ON's data model."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "transformation_id": {
                            "type": "string",
                            "description": "ID of the transformation to apply",
                        },
                        "input_text": {
                            "type": "string",
                            "description": "The text to transform",
                        },
                        "model_id": {
                            "type": "string",
                            "description": "ID of the LLM model to use (e.g. the 35B teacher)",
                        },
                    },
                    "required": ["transformation_id", "input_text", "model_id"],
                },
            },
            {
                "name": "create_note",
                "description": (
                    "Create a note in an Open Notebook notebook. Notes are visible in the ON web UI "
                    "and can be used as semantic index entries visible to both humans and agents."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "notebook_id": {
                            "type": "string",
                            "description": "The notebook ID to add the note to",
                        },
                        "title": {
                            "type": "string",
                            "description": "Note title",
                        },
                        "content": {
                            "type": "string",
                            "description": "Note content (markdown)",
                        },
                        "note_type": {
                            "type": "string",
                            "enum": ["human", "ai"],
                            "description": "Note type: 'ai' for model-generated, 'human' for manual (default: 'ai')",
                        },
                    },
                    "required": ["notebook_id", "content"],
                },
            },
            {
                "name": "search_notes",
                "description": (
                    "Search across ON's sources and notes using text or vector search. "
                    "Returns matching content from the semantic index."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["text", "vector"],
                            "description": "Search type: 'text' for keyword, 'vector' for semantic (default: 'text')",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results (default: 20)",
                        },
                        "search_sources": {
                            "type": "boolean",
                            "description": "Include sources in results (default: true)",
                        },
                        "search_notes": {
                            "type": "boolean",
                            "description": "Include notes in results (default: true)",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]
        self.send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})

    def _handle_tools_call(self, msg_id, params):
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info(f"Tool call: {name}({list(arguments.keys())})")

        try:
            result = self._dispatch(name, arguments)
            self.send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                    "isError": False,
                },
            })
        except Exception as e:
            logger.error(f"Tool error: {e}")
            self.send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            })

    def _dispatch(self, name: str, args: dict) -> dict:
        if name == "query_librarian":
            return self.lib.query(**args)
        elif name == "check_freshness":
            return self.lib.check_freshness(**args)
        elif name == "get_dependency_graph":
            return self.lib.get_dependency_graph()
        elif name == "list_modules":
            return self.lib.list_modules(**{k: v for k, v in args.items() if v is not None})
        elif name == "reload_librarian":
            self.lib.reload()
            return {"status": "reloaded"}
        elif name == "pipeline_status":
            return self.lib.pipeline_status()
        elif name == "create_or_get_notebook":
            return self._create_or_get_notebook(**args)
        elif name == "ingest_source":
            return self._ingest_source(**args)
        elif name == "run_transformation":
            return self._run_transformation(**args)
        elif name == "create_note":
            return self._create_note(**args)
        elif name == "search_notes":
            return self._search_notes(**args)
        else:
            raise ValueError(f"Unknown tool: {name}")

    def _create_or_get_notebook(self, name: str, description: str = "") -> dict:
        notebooks = self.on.notebooks_list()
        for nb in notebooks:
            if nb["name"] == name:
                return nb
        nb = self.on.notebook_create(name=name, description=description)
        logger.info(f"Created notebook: {nb['id']} ({name})")
        return nb

    def _ingest_source(
        self,
        notebook_id: str,
        content: str,
        title: str | None = None,
        embed: bool = True,
    ) -> dict:
        source = self.on.source_create_text(
            content=content,
            title=title,
            notebooks=[notebook_id],
            embed=embed,
            async_processing=True,
        )
        logger.info(f"Ingested source: {source.get('id', '?')} — {title}")
        return source

    def _run_transformation(
        self, transformation_id: str, input_text: str, model_id: str
    ) -> dict:
        result = self.on.transformation_execute(
            transformation_id=transformation_id,
            input_text=input_text,
            model_id=model_id,
        )
        logger.info(f"Transformation {transformation_id} complete")
        return result

    def _create_note(
        self,
        notebook_id: str,
        content: str,
        title: str | None = None,
        note_type: str = "ai",
    ) -> dict:
        note = self.on.note_create(
            content=content,
            title=title,
            note_type=note_type,
            notebook_id=notebook_id,
        )
        logger.info(f"Created note: {note.get('id', '?')} — {title}")
        return note

    def _search_notes(
        self,
        query: str,
        search_type: str = "text",
        limit: int = 20,
        search_sources: bool = True,
        search_notes: bool = True,
    ) -> dict:
        return self.on.search(
            query=query,
            search_type=search_type,
            limit=limit,
            search_sources=search_sources,
            search_notes=search_notes,
        )


if __name__ == "__main__":
    MCPServer().run()