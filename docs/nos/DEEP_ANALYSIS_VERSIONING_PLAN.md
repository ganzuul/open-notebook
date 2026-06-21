# Deep Analysis Versioning & Determinism Plan

**Status**: Draft — planning phase, no code changes to run yet.  
**Date**: 2026-06-21  
**Author**: nos + opencode  

---

## 1. Problem Description

The LaserCortex pipeline produces two kinds of LLM-generated artifacts:

1. **Deep Analysis notes** (Pass 2): Per-file semantic index entries produced by the 35B model via the Context-Compiler-V1 transformation. Stored as ON notes titled `"{module} — Deep Analysis"`.
2. **Cross-layer edges** (Pass 3): Cross-layer dependency links produced by the 35B model via the Cross-Layer-Linker transformation. Stored as `PASS3_EDGES.json` and merged into `DEPENDENCY_GRAPH.json`.

Both artifacts are currently **non-deterministic**: the 35B model runs at `temperature=1.0` (the ON/Esheranto default), so the same input produces different output each run. This creates several problems:

### 1a. Cache Invalidation Without Versioning

`.phonebook_cache.json` tracks completion using `transform_sha` — a truncated SHA256 of the **source file content**. If a file's content hash matches, the pipeline skips it. But:

- Changing generation parameters (temperature, seed, model version) changes the **output** without changing the **input hash**.
- The cache says "already done" → the pipeline skips the file → old output is preserved even though it was generated with different parameters.
- There is no mechanism to re-run files that were processed with an older parameter set.

**Current state**: 1397 files have Deep Analysis notes generated at `temperature=1.0`. Changing to `temperature=0` would produce different output for all of them, but the cache would say "skip" for all of them.

### 1b. No Provenance Tracking

The cache entry schema:
```json
{
  "transform_sha": "4f6395c936202dc4",   // first 16 chars of file SHA256
  "_transformed": true,
  "_transformed_at": "2026-06-17T17:57:36"
}
```

There is no record of:
- What model produced this output (model name, quantization, version)
- What generation parameters were used (temperature, seed, max_tokens)
- Which transformation prompt version was active
- The SHA256 of the **output** itself (only the input is hashed)

### 1c. Stochastic Resonance Concern

There exists an effect in signal processing where **introducing noise improves signal quality** (stochastic resonance). We do not yet know whether LLMs are subject to this effect. Setting `temperature=0` (greedy decoding) might produce:
- **More consistent** output (same input → same output) ✅
- **Lower quality** output (greedy decoding can get stuck in local optima) ❓
- **Less diverse** coverage of edge cases in cross-layer linking ❓

We should **measure before we decide**.

---

## 2. Current Approach: Shortcomings

### 2.1 The `transform_sha` Is Input-Only

| What | Hash of | Problem |
|------|---------|---------|
| `transform_sha` | File content (input) | Same file → "skip" regardless of parameter changes |
| Output hash | Not tracked | Can't detect if output changed between runs |
| Model params | Not tracked | Can't know which parameters produced a given output |

### 2.2 No Output Versioning in ON Notes

ON notes have fields: `id`, `title`, `content`, `note_type`, `created`, `updated`, `command_id`.

- There is no `generation_params` field.
- A note titled `"README — Deep Analysis"` will be overwritten on re-run with no history.
- The `updated` timestamp changes but the previous content is lost.

### 2.3 Cross-Layer Edges Are Ephemeral

`PASS3_EDGES.json` accumulates edges across runs (via `_pass` field and cumulative merge), but:

- Stochastic 35B output means different edges each run.
- There is no way to tell if an edge was discovered at `temperature=1.0` or `temperature=0`.
- Layer assignments (`layer_source`, `layer_target`) depend on cache lookups that use module names from the 35B output, which vary between runs.

### 2.4 The Context Window Problem

Pass 3 (Cross-Layer Linker) must process all module abstracts together. Current approach: truncate to 180K chars (~50K tokens).

- Truncation is lossy — modules near the cutoff are dropped regardless of relevance.
- The 35B never sees modules it doesn't receive, so it can't find edges involving them.
- Increasing context isn't a fix — it just raises the ceiling.

---

## 3. Hypotheses to Test

### H1: `temperature=0` produces deterministic output for the same input

**Test**: Run the same file through Context-Compiler-V1 twice with `temperature=0, seed=<content_hash>`. Compare outputs character-by-character.

**Expected**: Identical output both times.

**If confirmed**: We can use `seed` derived from content hash for reproducibility, and `transform_sha` becomes a reliable cache key.

### H2: `temperature=0` produces equal or better quality than `temperature=1.0`

**Test**: Run 10 diverse files through Context-Compiler-V1 at both temperatures. Compare:
- Completeness (all required fields present?)
- Accuracy (do tags/contracts match the source code?)
- Signal-to-noise ratio (less filler = better)

**Measurement**: Manual review of 5 FORMALIZATION files, 5 API_GATEWAY files.

**If `temperature=0` is worse**: We need stochastic resonance — introduce controlled randomness via `seed=random` while keeping temperature low (e.g., `temperature=0.3`).

### H3: Embedding similarity can pre-filter cross-layer pairs

**Test**: 
1. Embed all enriched module abstracts using bge-m3 (port 8082).
2. Compute cosine similarity for all cross-layer pairs (modules from different layers).
3. Check whether the 7 known cross-layer edges appear in the top-100 similar pairs.

**Expected**: 6/7 edges with shared lexical terms should rank highly. The `Cost→NodeCost` edge (no lexical overlap) may or may not appear.

**If confirmed**: Replace the monolithic 35B call with embedding pre-filter + per-pair 35B verification.

### H4: Per-pair 35B calls produce better cross-layer edges than monolithic

**Test**: Run Cross-Layer-Linker on individual high-similarity pairs (from H3) with full context for each pair (~1-2K tokens per pair). Compare edge quality against the current monolithic approach.

**Expected**: Per-pair calls should:
- Never overflow context window
- Get full enriched text for both modules
- Produce `invariant_at_boundary` and `failure_mode` fields more reliably
- Generate correct layer assignments (no UNKNOWN labels)

---

## 4. Proposed Versioning Scheme

### 4.1 Generation Manifest

Add a generation manifest to each cache entry:

```json
{
  "transform_sha": "4f6395c936202dc4",
  "transform_version": 2,
  "transform_params": {
    "temperature": 0,
    "seed_mode": "content_hash",
    "model": "Qwen3.6-35B-A3B-Q4_K_M",
    "transformation_id": "transformation:0tkrn2ru01xj0zd4cp09",
    "max_tokens": 8192
  },
  "transform_output_sha": "abc123...",,
  "_transformed": true,
  "_transformed_at": "2026-06-21T12:00:00Z"
}
```

**Key fields**:
- `transform_version`: Incremented when generation parameters change. Pipeline re-runs files where `transform_version < CURRENT_VERSION`.
- `transform_params`: The full parameter set used. Enables provenance tracking.
- `transform_output_sha`: SHA256 of the 35B output. Detects when output changes between runs even with the same parameters.

### 4.2 Version Migration

On pipeline startup:
1. Read `transform_version` from all cache entries.
2. If the current pipeline version > cached version, mark those files for re-processing.
3. Old notes are **preserved** — create a new note with `(v2)` suffix, keep the old one.
4. After verification (H1-H2), update `CURRENT_VERSION` in the pipeline script.

### 4.3 ON Note Versioning

Instead of overwriting notes titled `"{module} — Deep Analysis"`:
- Title format: `"{module} — Deep Analysis (v{N})"`
- Or: append a `generation_params` field to the note's content (structured metadata section)
- Old versions remain searchable in ON.

### 4.4 Edge Versioning

`PASS3_EDGES.json` gains a `_version` and `_params` field per edge:

```json
{
  "edge_id": "E001",
  "edge_type": "DATA_SOURCE",
  ...
  "_pass": 3,
  "_version": 2,
  "_params": {
    "temperature": 0,
    "seed_mode": "content_hash",
    "model": "Qwen3.6-35B-A3B-Q4_K_M"
  }
}
```

Edges from different versions coexist. A `CURRENT_EDGE_VERSION` flag determines which version the pipeline considers authoritative.

---

## 5. Implementation Order

### Phase 0: Do No Harm (before any parameter changes)

- [ ] **P0.1**: Add `transform_output_sha` to cache entries (compute from existing output, don't re-run)
- [ ] **P0.2**: Add `transform_version` and `transform_params` to cache entries (version=1 for current temp=1.0 entries)
- [ ] **P0.3**: Update `generate_phonebook.py` to write version metadata on new transforms
- [ ] **P0.4**: Set `CURRENT_VERSION = 1` in pipeline script (no change in behavior)

### Phase 1: Test Determinism (H1)

- [ ] **P1.1**: Run 3 files through Context-Compiler-V1 at `temperature=0, seed=<hash>` twice each
- [ ] **P1.2**: Compare outputs character-by-character
- [ ] **P1.3**: Document results in this file

### Phase 2: Test Quality (H2)

- [ ] **P2.1**: Select 10 diverse files (5 FORMALIZATION, 5 API_GATEWAY)
- [ ] **P2.2**: Run each at `temperature=0` and `temperature=1.0`
- [ ] **P2.3**: Manual review: completeness, accuracy, signal-to-noise
- [ ] **P2.4**: If `temperature=0` is worse, test `temperature=0.3` (H2 extended)

### Phase 3: Test Embedding Pre-Filter (H3, H4)

- [ ] **P3.1**: Embed all enriched module abstracts via bge-m3
- [ ] **P3.2**: Compute cross-layer cosine similarity matrix
- [ ] **P3.3**: Verify known edges appear in top-100 similar pairs
- [ ] **P3.4**: Run per-pair Cross-Layer-Linker on top-50 pairs
- [ ] **P3.5**: Compare edge quality against monolithic approach

### Phase 4: Parameter Change (only after H1-H2 confirmed)

- [ ] **P4.1**: Set `CURRENT_VERSION = 2`, `transform_params.temperature = 0`
- [ ] **P4.2**: Pipeline re-runs all version-1 files with version-2 parameters
- [ ] **P4.3**: Old notes preserved with `(v1)` suffix
- [ ] **P4.4**: New notes created with `(v2)` suffix

### Phase 5: Hybrid Pass 3 (only after H3-H4 confirmed)

- [ ] **P5.1**: Implement embedding-based pair pre-filter
- [ ] **P5.2**: Replace monolithic 35B call with per-pair verification
- [ ] **P5.3**: Update `PASS3_EDGES.json` schema with version/params
- [ ] **P5.4**: Remove context-window truncation

---

## 6. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| `temperature=0` degrades quality | Re-run costs 20+ hours | Test on 10 files first (Phase 2) |
| Stochastic resonance is real | Deterministic output is worse than stochastic | Use `temperature=0.3` with deterministic seed |
| Embedding pre-filter misses edges | False negatives in cross-layer links | Keep threshold low; manual review |
| bge-m3 OOM under concurrency | INC-1 recurrence | Use single-worker mode with memory limits |
| 1397 files need re-run on version bump | 20+ hours of 35B time | Incremental: only re-run priority files first |

---

## 7. Current State

- **1397 files** with Deep Analysis notes (generated at `temperature=1.0`)
- **7 cross-layer edges** in `PASS3_EDGES.json` (generated at `temperature=1.0`)
- **`transform_sha` cache**: Input-only, no output hash, no params tracking
- **No versioning**: Changing parameters would silently skip all cached files
- **Context window overflow**: Pass 3 truncated from 332K chars (72K tokens) to 180K chars (50K tokens)

**Next action**: Phase 0 (P0.1–P0.4) — add versioning metadata to cache before any parameter changes.