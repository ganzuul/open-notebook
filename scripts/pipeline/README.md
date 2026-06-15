# Librarian Pipeline

A two-tier semantic compression pipeline that uses Open Notebook as the orchestration layer.

## Architecture

```
Opencode (9B student) ──MCP──► Librarian Bridge ◄──REST──► Open Notebook (5055)
                                                              │
                                                              ├── Sources (file content)
                                                              ├── Notes (semantic index entries)
                                                              ├── Transformations (35B teacher)
                                                              └── Search (vector + text)

Librarian Server (8081) ──► Phonebook JSON (fast tag/layer lookup)
```

The 35B teacher model is accessed **exclusively** through ON's Transformation API — never directly. The 9B student model (Opencode) queries the librarian via MCP tools.

## Reproduction

### Prerequisites

- Docker Compose (for Open Notebook)
- llama-server binary (for 35B/9B models)
- Python 3.10+ with `requests`, `fastapi`, `uvicorn`, `pydantic`
- `just` (command runner): `curl -sSf https://just.systems/install.sh | bash -s -- --to ~/.local/bin`

### Step 1: Start services

```bash
# Start Open Notebook (includes SurrealDB)
just on-up

# Start 35B teacher model on :8080 (in a separate terminal)
just llama-35b

# Start librarian index server on :8081
just librarian-start
```

### Step 2: Bootstrap ON

```bash
# Creates Context-Compiler-V1 and Cross-Layer-Linker transformations,
# registers the 35B model credential, verifies all services
just bootstrap

# Check everything is ready
just status
```

### Step 3: Index a repository

```bash
# Fast mode (heuristic only, no LLM):
just pipeline-fast /path/to/repo

# Full 3-pass pipeline (35B teacher, ~100s per file):
just pipeline-full /path/to/repo
```

### Step 4: Configure Opencode

Add to `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "mcp": {
    "lean-lsp": { "type": "local", "command": ["uvx", "lean-lsp-mcp"], "enabled": true },
    "open-notebook-librarian": {
      "type": "local",
      "command": ["python3", "/path/to/open-notebook/scripts/pipeline/mcp_librarian_server.py"],
      "enabled": true
    }
  },
  "agent": {
    "system": "You have access to the Open Notebook Librarian via MCP tools. BEFORE reading code files directly, use query_librarian or search_notes to find relevant modules. ..."
  }
}
```

## 3-Pass Pipeline

Per the spec (see `docs/nos/Gemini_and_Claude_on_implementation.md`):

| Pass | Name | What it does | Mode |
|------|------|-------------|------|
| 1 | **Map** | Heuristic analysis + ON source ingest + heuristic notes | All modes |
| 2 | **Reduce** | 35B Context-Compiler-V1 transformation per file → Deep Analysis notes | `--mode full` only |
| 3 | **Link** | Cross-Layer-Linker batches grouped abstracts → typed dependency edges | `--mode full` only |

## Files

| File | Purpose |
|------|---------|
| `generate_phonebook.py` | 3-pass pipeline: ingest sources → transform → create notes |
| `mcp_librarian_server.py` | MCP bridge: 10 tools (5 librarian + 5 ON API) |
| `librarian_server.py` | FastAPI index server on :8081 with `/sync-from-on` endpoint |
| `bootstrap_on.py` | Idempotent ON setup: transformations, model, credential |
| `justfile` | Operational commands (`just status`, `just bootstrap`, etc.) |
| `flake.nix` | Nix flake: dev shell, apps for `nix run` |
| `PHONEBOOK_SYSTEM_PROMPT.md` | System prompt for 9B student KV-cache |

## Spec Documents

The pipeline was designed against three spec documents in `docs/nos/`:

- `Gemini_on_OpenNotebook.md` — ON as the orchestration layer, staged compilation pipeline
- `Gemini_and_Claude_on_implementation.md` — Typed dependency edges, cross-layer query grammar, student model caching
- `install_on_Alpha.md` — Hardware config, 9B/35B model topology, cache optimization