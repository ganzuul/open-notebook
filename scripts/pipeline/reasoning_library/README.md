# Reasoning Library

A general framework for **accelerating structured LLM classification tasks**
by accumulating reasoning traces, compressing them into reusable scripts,
and routing new inputs to the cheapest adequate path.

**Three-tier routing:**
1. **Hardcoded rule** — instant match, no LLM call
2. **Script centroid** — compressed system prompt, ~20s LLM call
3. **Fallback** — full reasoning, ~60s LLM call

With a reasoning model (e.g. Qwen3.6-35B), scripts reduce reasoning tokens
by **~97%** (24 words vs 1015 mean) — the model applies pre-baked criteria
instead of re-deriving the classification framework each time.

## What this is meant to be

The reasoning library solves a specific problem: **you have an expensive
reasoning model that you call repeatedly with structurally similar inputs.**
Each call wastes most of its budget re-deriving the same reasoning strategy.
The library captures that strategy once, compresses it, and reuses it.

This is **not** a fine-tuning or distillation framework. The model retains
its full capability — it just starts from a better system prompt. Think of
it as **pre-compiling the mental framework** so the model can focus on the
pair-specific judgment.

### Concrete example: cross-layer dependency discovery

In our LaserCortex project, we check whether a Python API module has a
semantic dependency on a TypeScript presentation component. Each verification
asks: "Is there a dependency, and if so, is it a SPECIFICATION, CONSTRAINT,
or DATA_SOURCE?"

Without the library, the 35B spends ~2700 tokens re-deriving:
- What each edge type means
- How to tell them apart
- What the invariant/failure-mode format looks like
- Self-correction: "wait, that's more of a CONSTRAINT than a SPECIFICATION"

With the library, it spends ~24 tokens: the script's system prompt contains
the distilled criteria from 20 previous examples. The model just applies them.

## What it can do

### Task-agnostic core

Swap the `TaskConfig` and you get a different domain. The same code works for:

```python
# Cross-layer dependencies
task = TaskConfig(
    name="cross-layer-dependency",
    input_fields={"source_module": "...", "target_module": "...", ...},
    output_taxonomy={"edge_type": {"SPECIFICATION": "...", ...}},
    format_instruction="DEPENDENCY|EDGE_TYPE|... or NONE",
    positive_marker="DEPENDENCY",
    negative_marker="NONE",
)

# API endpoint ↔ database table
task_api_db = TaskConfig(
    name="api-db-dependency",
    input_fields={"endpoint": "...", "table": "...", ...},
    output_taxonomy={"access_type": {"READ": "...", "WRITE": "...", ...}},
    format_instruction="ACCESS|READ|... or NO_ACCESS",
    positive_marker="ACCESS",
    negative_marker="NO_ACCESS",
)
```

### Self-improving loop

1. Run N examples with full reasoning → collect **traces**
2. Compress similar traces into **scripts** (one meta-35B call per cluster)
3. Route new inputs: hardcoded → script → fallback
4. New traces refine existing scripts or create new ones

### Meta-compression

The `MetaCompressor` takes 3+ traces for the same input profile and produces
a ~500-token system prompt that captures the shared reasoning strategy. It
extracts:
- **Decision criteria**: what distinguishes the output classes
- **Common reasoning patterns**: how the model thinks about these inputs
- **Invariant templates**: recurring structural constraints
- **Failure mode patterns**: common gotchas and edge cases

### Embedding-based routing

Inputs are embedded (via any bge-m3 endpoint) and matched against script
centroids via cosine similarity. Similar inputs get similar scripts.

## Capability gaps

### 1. Reasoning model dependency (fundamental)

The library saves tokens but **cannot eliminate reasoning_content** when the
underlying model is a thinking model like Qwen3.6-35B. The model always
produces a reasoning trace. The library makes it shorter (24 words vs 1015),
not absent.

**If you pair this with a non-reasoning model** (e.g. Llama 3, GPT-4o-mini),
scripts are even more effective because there's no reasoning_content at all
— the system prompt directly determines the output.

### 2. Cold-start problem

You need **≥3 traces per cluster** before meta-compression works. Initially,
everything falls through to fallback. The library becomes useful only after
processing enough examples to form clusters.

**Mitigation**: seed with hardcoded rules (domain expert writes patterns)
and use broad layer-pair scripts (less specific, but still better than
generic fallback).

### 3. Centroid quality depends on embedding quality

Script routing uses bge-m3 centroids. If the embedding space doesn't separate
your input types well, centroid matching degrades. On our cross-layer data,
most pairs had max similarity ~0.60-0.66 to any centroid (too low for 0.70
threshold). We added **layer-pair fallback** (Tier 3) to handle this: if no
centroid matches, but the layer pair matches a script, use that script anyway.

**For new domains**: check the similarity distribution before setting the
threshold. Expect to add a heuristic fallback for common input profiles.

### 4. No online learning (yet)

The library saves to disk and loads at startup. There's no incremental
centroid update or streaming trace processing. To add new traces and
regenerate scripts, you must re-run the compressor.

**Workaround**: the `library.save()` / `ReasoningLibrary()` constructor
round-trips cleanly. Run the compressor periodically (e.g. nightly) to
incorporate new traces.

### 5. Single-task per library instance

A `ReasoningLibrary` instance handles one `TaskConfig`. Multi-task setups
(verify dependencies AND classify API access patterns) need separate
instances with separate script stores.

**This is by design**: separating task domains avoids cross-contamination
of reasoning strategies. But it means more boilerplate for multi-task
workflows.

### 6. Embed server dependency

Centroid matching requires a running bge-m3 embed server. Without it,
Tier 2 (script centroid) is skipped and Tier 3 (layer-pair fallback) is
the best you get. The library degrades gracefully but loses its primary
routing signal.

**Mitigation**: layer-pair fallback ensures every pair with a matching
layer pair gets a script assignment. Only truly novel layer pairs fall
through to full reasoning.

### 7. No quality assurance for scripts

Meta-compression is a single-pass 35B call. There's no validation that
the compressed script captures the correct reasoning strategy. A bad
compression produces a misleading system prompt that degrades downstream
results.

**Watch for**: scripts that produce *longer* reasoning than fallback
(a sign the compression was poor). The confidence score (fraction of
source traces that agreed on the edge type) is a weak proxy for script
quality — it tells you the source data was consistent, not that the
compression captured it correctly.

## Architecture

```
scripts/pipeline/reasoning_library/
├── __init__.py      # Package exports
├── models.py        # TaskConfig, ReasoningTrace, ReasoningScript,
│                    # HardcodedRule, RoutingDecision
├── library.py       # ReasoningLibrary — persistent storage,
│                    # save/load, trace management
├── compressor.py    # MetaCompressor — trace → compressed script,
│                    # DEFAULT_SYSTEM_PROMPT
└── router.py        # EmbeddingRouter — three-tier routing cascade
```

## Quick start

```python
from reasoning_library import (
    TaskConfig, ReasoningTrace, ReasoningScript,
    HardcodedRule, ReasoningLibrary, MetaCompressor,
    EmbeddingRouter, DEFAULT_SYSTEM_PROMPT,
)

# 1. Define your task
task = TaskConfig(
    name="my-task",
    description="Classify pairs as X, Y, or nothing",
    input_fields={"left": "Left entity", "right": "Right entity"},
    output_taxonomy={
        "kind": {"X": "...", "Y": "..."},
    },
    format_instruction="KIND|X|DETAILS or NONE",
    positive_marker="KIND",
    negative_marker="NONE",
)

# 2. Create a library
lib = ReasoningLibrary("/tmp/my_library.json")

# 3. Add traces (from your LLM calls)
trace = ReasoningTrace(
    pair_key="a:b",
    inputs={"left": "a", "right": "b"},
    reasoning_content="...",
    final_content="KIND|X|some detail",
    result={"kind": "X", "detail": "some detail"},
    is_positive=True,
)
lib.add_trace(trace)
lib.save()

# 4. Compress traces into scripts (do this after ≥3 per cluster)
compressor = MetaCompressor(llm_api="http://localhost:8080")
scripts = compressor.compress_scripts(lib, task, min_traces=3)

# 5. Route new inputs
router = EmbeddingRouter(script_threshold=0.60)
decision = router.route(
    inputs={"left": "c", "right": "d"},
    pair_embedding=None,  # bge-m3 embedding (optional)
    task_config=task,
    hardcoded_rules=lib.get_rules(task.input_schema_hash()),
    scripts=lib._scripts,
    script_centroids=lib._script_centroids,
)
```
