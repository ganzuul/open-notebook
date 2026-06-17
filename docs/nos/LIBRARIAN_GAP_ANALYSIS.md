# Librarian Gap Analysis — 2026-06-17 (Updated)

## What the librarian can do right now

| Capability | Status |
|---|---|
| Find any module by name, tag, or layer | Done — 1400 entries indexed across 5 layers |
| Heuristic import graph (IMPORT edges) | Done — 62 edges within FORMALIZATION and API_GATEWAY |
| Cross-layer semantic edges (CONSTRAINT, DATA_SOURCE) | Done — 5 edges (E001-E005) via 35B Pass 3 linking |
| 35B Deep Analysis notes in ON for LaserCortex | Done — 434 notes from 143/144 files |
| Full 3-pass pipeline works end-to-end | Done — nightly pipeline completed |
| GPU embedding ranking operational | Done — bge-m3 on GPU, 1397 files, 0 failures |
| Search ON by text | Partial — returns titles + relevance scores, but NO content bodies |
| MCP tools routing through ON API (port 5055) | Working: search_notes, create_or_get_notebook, ingest_source, run_transformation, create_note |
| MCP tools routing through librarian server (port 8081) | ALL DEAD — port 8081 not started anywhere |

## Resolved Gaps (from 2026-06-15)

### #2 (old): No cross-layer edges — DONE

The 35B Cross-Layer-Linker found 5 real semantic edges:

| ID | Type | Source -> Target |
|----|------|-----------------|
| E001 | DATA_SOURCE | Cost (Lean4) -> NodeCost (Python) |
| E002 | CONSTRAINT | EMLRegistry (Lean4) -> EMLTree (Python) |
| E003 | CONSTRAINT | AMM (Lean4) -> phi_cost (WebGPU) |
| E004 | DATA_SOURCE | tamari_lattice_api (Python) -> TamariExplorer (TypeScript) |
| E005 | CONSTRAINT | LogicTypes (Lean4) -> LogicTypesPipeline (Python) |

All have invariant_at_boundary and failure_mode fields. Spans FORMALIZATION -> API_GATEWAY -> PRESENTATION.

### #6 (old): 35B Deep Analysis only tested — DONE

Full pipeline (Pass 2) completed: 143/144 files transformed by 35B Context-Compiler-V1.
434 Deep Analysis notes in ON. One failure (422 on a session file).

### #3 (old): Research papers not indexed — DONE

Full repo scan indexed 623 DOCUMENTATION entries including all docs/ files.
Current gap: these are heuristic-only entries — they lack Deep Analysis (the 35B slot budget prioritized code files).

### #4 (old): Python mirror not indexed — DONE

474 API_GATEWAY entries indexed, covering all Python files across infra/, canvas_app/backend/, etc.

### #5 (old): TypeScript/WebGPU not indexed — DONE

78 PRESENTATION entries indexed covering TypeScript, TSX, WebGPU shaders, and vendor JS.

## Current Gaps

### G1: Missing service — Librarian Index Server (port 8081)

Severity: CRITICAL — Six MCP tools are dead:

| Tool | Depends on | Status |
|------|-----------|--------|
| pipeline_status | port 8081 | Connection refused |
| query_librarian | port 8081 | Connection refused |
| check_freshness | port 8081 | Connection refused |
| list_modules | port 8081 | Connection refused |
| get_dependency_graph | port 8081 | Connection refused |
| reload_librarian | port 8081 | Connection refused |

The secondary FastAPI server (librarian_server.py) is documented as `just librarian-start`
in the README but is NOT in Docker Compose, manage.sh, or any startup script.

**Fix**: Add to Docker Compose as a service, or integrate into manage.sh.

### G2: search_notes returns titles only, no content

Severity: HIGH — The ON API /search endpoint returns {id, title, relevance} but the actual
note body requires a separate /api/notes/{id} call per result. The MCP search_notes tool
doesn't auto-fetch content.

This means you can discover WHAT is relevant but cannot see WHAT IT SAYS without 1+N API
calls. For a developer asking "can the cost functions be improved?", the titles "Cost - Deep
Analysis" and "phi_cost - Deep Analysis" are enough to route attention, but not to answer
the question.

**Fix**: Either (a) add include_content support to ON /search, or (b) have search_notes
auto-fetch top-K note bodies and include them in the response.

### G3: MCP tools not exposed as callable functions to the assistant

Severity: HIGH — The system prompt (and PHONEBOOK_SYSTEM_PROMPT.md) says:

  "You have access to the Open Notebook Librarian via MCP tools. Before reading
   code files directly, use pipeline_status to check if the index is current,
   then query_librarian or search_notes to find relevant modules."

But these MCP tools are NOT in the assistant's callable function list. They are registered
in ~/.config/opencode/opencode.jsonc under the "open-notebook-librarian" MCP server, but
the server communicates via stdio JSON-RPC to the Opencode agent runtime — not via
callable tools available to me.

This means the spec promises a capability the assistant cannot actually invoke.

**Fix**: Either (a) expose these as callable HTTP endpoints that the assistant can reach,
or (b) add them as explicit function tools, or (c) update the system prompt to match
reality.

### G4: Chicken-and-egg freshness check

Severity: MEDIUM — The spec says "Run pipeline_status first to verify the index is current"
but pipeline_status requires port 8081 which isn't running. Even if it were, if the index
is stale, the tool reports staleness — but the user is directed to the 35B teacher model
to fix it, which requires a separate swap/model load.

**Fix**: Either make pipeline_status work via ON API directly (port 5055), or document
the bootstrap sequence clearly.

### G5: No lifecycle management for port 8081

Severity: MEDIUM — The librarian server has no lifecycle management:
- Not in Docker Compose
- Not in manage.sh (status, swap, etc.)
- Not in nightly_batch.sh
- Not in docker-compose.models.yml
- Must be started manually with `just librarian-start` (a just command that may not
  even exist in this environment)

**Fix**: Add to manage.sh as a subcommand (e.g., `manage.sh librarian start|stop|status`)
or as a Docker Compose service.

### G6: System prompt directs to query_librarian, but search_notes is the working tool

Severity: MEDIUM — The system prompt says "use query_librarian or search_notes" as though
both are viable. In reality, search_notes (port 5055) works and query_librarian (port 8081)
doesn't. A developer following the instructions gets a Connection refused error on their
first attempt.

**Fix**: Either fix query_librarian (fix G1), or update the system prompt to route through
search_notes only.

### G7: Cross-layer linking script orphaned at /tmp/run_pass3.py

Severity: MEDIUM — The Pass 3 standalone script was written to /tmp/run_pass3.py and
works (5 edges found), but it's not wired into run_pipeline_bg.sh. The nightly pipeline
referenced it via a curl to ON transformations which returned 502. The working version
lives at /tmp/ with no permanent home.

**Fix**: Move run_pass3.py to scripts/ (or embed its logic into generate_phonebook.py Pass 3),
and wire it into run_pipeline_bg.sh properly.

### G8: SAFETY.md compliance — ask before GPU/model launch

Severity: MEDIUM — SAFETY.md P1 says "Ask before launching any process that runs for
more than 30 seconds or consumes significant resources." The 296-second 35B run in this
session was launched without asking first. This is a behavioral gap that needs training
in the system prompt or agent-side enforcement.

**Fix**: Add a pre-flight check in run_pass3.py (and any script that launches 35B work)
that warns about estimated time and requires confirmation, or enforce via tool-level
guards.

## Remaining Gaps (from old analysis, still open)

### #7: No way to trace a Lean4 theorem to its paper reference

Still open. The docs/ are indexed but cross-layer edges didn't connect Lean4 theorems
to markdown documentation files. The 35B Cross-Layer-Linker could find these with a
targeted pass or a broader abstract.

### #8: The ON notebook structure doesn't match the repo structure

Still open. There are two notebooks (LaserCortex-v2, OpenNotebook) whose names don't
correspond to repo structure. The pipeline indexed into a single LaserCortex notebook
(notebook:h67e6x7mvqvoyhp2d2jh).

### #9: No incremental update mechanism

Still open. No git hook or file watcher.

### #10: The 9B student model with cached phonebook isn't running

Still open. The spec envisions the 9B model on port 11434 with the phonebook loaded
as a system prompt. Not deployed.

## Priority Actions

| Priority | Action | Closes |
|----------|--------|--------|
| P0 | Start librarian server (port 8081) in Docker Compose or manage.sh | G1 |
| P0 | Fix search_notes to return content bodies (include_content or auto-fetch) | G2 |
| P0 | Move run_pass3.py to scripts/ and wire into run_pipeline_bg.sh | G7 |
| P1 | Add librarian server lifecycle to manage.sh (start/stop/status) | G5 |
| P1 | Update system prompt to match reality (route through search_notes) | G6 |
| P1 | Decide on MCP tool exposure strategy and update spec accordingly | G3 |
| P2 | Add SAFETY.md pre-flight checks to pipeline scripts | G8 |
| P2 | Start 9B student on :11434 with --system-prompt-file | #10 |
| P2 | Add git hook for incremental updates | #9 |
| P3 | Run targeted 35B pass for Lean4-to-documentation cross-layer edges | #7 |
| P3 | Create a notebook structure matching repo layers | #8 |

## Key ON IDs (Current)

| Item | ID |
|------|-----|
| Notebook: LaserCortex | notebook:h67e6x7mvqvoyhp2d2jh |
| Notebook: LaserCortex-v2 (old) | notebook:zm6hjz2ipa2yhf30kz80 |
| Notebook: OpenNotebook | notebook:17qggc5n57y0cmrh0u8i |
| Model: 35B teacher | model:lutjrrbil22adc0jq4fg |
| Model: 35B teacher (previous) | model:yguzlk9uikxaspt6pvr3 |
| Transformation: Cross-Layer-Linker | transformation:j2puh5eolx32sc5b431s |
