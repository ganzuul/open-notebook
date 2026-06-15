# Librarian Gap Analysis — 2026-06-15

## What the librarian can do right now

| Capability | Status |
|---|---|
| Find any Lean4 module by name, tag, or layer | Done — `query_librarian` returns EMLRegistry with 10 contracts, 1 export |
| Check if a file changed since last indexing | Done — `check_freshness` with SHA256 |
| See the dependency graph | Done — 97 nodes, 194 edges (mostly Python import edges from ON's own codebase) |
| 35B Deep Analysis notes exist in ON for LaserCortex | Done — 18 notes in the LaserCortex-v2 notebook |
| Search ON by text | Done — but currently returning 0 for semantic queries like "associator pentagon Stasheff" |

## Gaps

### 1. Semantic search over Lean4 source content is empty

The `search_notes` query for "associator pentagon Stasheff" returned 0 results. The sources were ingested but not embedded (no embedding model configured). This means the ON vector search is useless for Lean4 content right now. The librarian's text search only works on the heuristic phonebook notes, not on the actual source code.

### 2. No cross-layer edges between Lean4, Python, and TypeScript

The dependency graph has 194 edges, but they're all `DATA_SOURCE` edges between ON's own Python files. Zero edges connect the Lean4 formalization layer to the Python mirror or to the TypeScript frontend. The heuristic analysis can only find `IMPORT` edges within a single language. The spec's whole value proposition — `CONSTRAINT` edges from Lean4 theorems to Python/TypeScript runtime, `MUTATION_TRIGGER` edges from Python to Lean4 invariants — is missing.

### 3. Research paper references are not indexed at all

The repo has ~20 unique paper references (arXiv papers, Tamari/Stasheff/Loday foundational works) and ~33 markdown documentation files, but none of them are in the LaserCortex notebook. The `docs/` directory phonebook was created in the `OpenNotebook` notebook, not the `LaserCortex` notebook, and those docs contain the research context that connects the Lean4 code to the underlying math.

### 4. The Python mirror (`infra/_cortex/`) is not indexed

The Python files that directly mirror Lean4 formalization (`_eml_tree.py`, `_logic_types.py`, `_cost.py`, `_amm.py`, etc.) are not in any notebook. These are the bridge between the formalization layer and the runtime layer. Without them indexed, there's no way to answer "which Python file implements the same EMLTree structure as EMLRegistry.lean?"

### 5. The TypeScript/WebGPU code is not indexed

The `canvas_app/` has ~55 TypeScript/TSX files and WebGPU shaders for visualizing the Tamari lattice and computing Φ on GPU. These have `#memory-topology` and `#webgpu-buffer` tags in the spec, but the actual files were ingested into the `LaserCortex-v2` notebook from the `LaserCortex/LaserCortex/` directory which only contains `.lean` files. The TypeScript source isn't in any notebook.

### 6. The 35B Deep Analysis has only been tested, not run for real

We tested the Context-Compiler-V1 transformation on one file (AMM.lean) and it produced excellent output (Tamari rotation connection, cross-refs to Cost/LogicTypes). But the full pipeline (`--mode full`) hasn't been run on all 18 Lean4 files. Each file takes ~100s with the 35B model, so that's ~30 minutes. The cross-layer linker (Pass 3) hasn't been run at all.

### 7. No way to trace a Lean4 theorem to its paper reference

If formalizing Stasheff's pentagon identity, I need to know: which doc discusses it? Which Python file implements the corresponding computation? Which TypeScript component visualizes it? Right now these connections exist only in human memory, not in any indexed system.

### 8. The ON notebook structure doesn't match the repo structure

There are two notebooks (LaserCortex-v2, OpenNotebook) whose names don't correspond to the actual repo structure. The repo has three coherent layers:
- **Lean4** (`LaserCortex/*.lean`) — formalization
- **Python** (`infra/_cortex/`, `canvas_app/backend/`) — mirror + runtime
- **TypeScript/WebGPU** (`canvas_app/frontend/`) — visualization

Each should be a separate notebook or at least clearly delineated in the same notebook.

### 9. No incremental update mechanism

The spec wants a git hook or file watcher that triggers `--incremental` when files change. Right now you have to manually run `just pipeline-incremental`.

### 10. The 9B student model with cached phonebook isn't running

The spec envisions the 9B model on port 11434 with the phonebook loaded as a system prompt via `--system-prompt-file`, giving the agent instant architectural awareness. This isn't deployed yet.

## Priority actions

| Priority | Action | Closes gap |
|---|---|---|
| **P0** | Run `--mode full` on all 18 Lean files (~30 min) | #6 |
| **P0** | Index `docs/` research papers into the LaserCortex notebook | #3 |
| **P0** | Index `infra/_cortex/` Python mirror into the LaserCortex notebook | #4 |
| **P1** | Configure an embedding model in ON for vector search | #1 |
| **P1** | Run Pass 3 (Cross-Layer-Linker) to produce `CONSTRAINT`/`MUTATION_TRIGGER` edges | #2 |
| **P1** | Index `canvas_app/` TypeScript/WebGPU files | #5 |
| **P2** | Start 9B student on :11434 with `--system-prompt-file` | #10 |
| **P2** | Add git hook for incremental updates | #9 |
| **P2** | Create a notebook structure matching repo layers | #8 |
| **P3** | Wire ON Notes to Lean4 docstrings (bidirectional link) | #7 |

## Repo Inventory Summary

| Category | Count | Location |
|----------|-------|----------|
| Lean 4 library files | 17 | `LaserCortex/*.lean` |
| Lean 4 standalone | 2 | `unified_spacetime_engine_explicit.lean`, `Visualization/loday_coordinates.lean` |
| TypeScript/TSX (canvas frontend) | ~55 | `canvas_app/frontend/src/` |
| JavaScript (vendors) | 8 | `blueprint/web/js/` |
| Python (cortex mirror) | 16 | `infra/_cortex/` |
| Python (canvas backend) | ~60 | `canvas_app/backend/` |
| Python (infra core/examples/tests) | ~70 | `infra/core/`, `infra/examples/`, `infra/tests/` |
| Documentation markdown | 33 | `docs/` |
| Research paper references | ~20 | `docs/*.pdf`, `documentation/paper/`, `references.bib` |
| Mathlib dependency | 1 | mathlib4 |

## Key ON IDs

| Item | ID |
|------|-----|
| Notebook: LaserCortex-v2 | `notebook:zm6hjz2ipa2yhf30kz80` |
| Notebook: OpenNotebook | `notebook:17qggc5n57y0cmrh0u8i` |
| Model: 35B teacher | `model:yguzlk9uikxaspt6pvr3` |
| Credential: Local Llama | `credential:6zath9dm6a5vls4uhuzt` |
| Transformation: Context-Compiler-V1 | `transformation:0tkrn2ru01xj0zd4cp09` |
| Transformation: Cross-Layer-Linker | `transformation:j2puh5eolx32sc5b431s` |