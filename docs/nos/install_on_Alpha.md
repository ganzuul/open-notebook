This is a solid architectural concept — a two-tier semantic compression pipeline where a heavier model does the expensive indexing work once, and a lighter cached model serves as a cheap routing layer for an agent. Let me push on what's strong and where it has real friction.

## What's Working Well

**The core insight is sound.** Separating "comprehension" (ON's 35B pass) from "navigation" (OC's 7B lookup) maps well onto the actual cost structure of LLM inference. The phone book metaphor is apt — you're essentially building a pre-compiled symbol table for your multi-paradigm stack.

**The tagging taxonomy is well-chosen.** `#memory-topology`, `#proof-bound`, `#mutation` — these cross-layer tags are where the real value is. A WebGPU buffer that correlates with a Lean4 invariant is exactly the kind of cross-cutting dependency that gets lost in naive RAG setups.

---

## The Friction Points Worth Addressing

**1. The 3-pass pipeline has a staleness problem.**
Your repo changes. Pass 1→2→3 produces a static artifact (`MASTER_RECON_PHONEBOOK.md`), but if a Django model changes its FK relationship or a WGSL shader gets a new bind group, the phonebook silently lies. You need an **invalidation strategy** built into the design — at minimum, file-hash-based cache keys per module block so OC knows which coordinate entries are stale before trusting them.

**2. The 7B model as a "routing engine" is fragile.**
Routing over a text phone book via a 7B model is relying on that model to reliably parse your structured blocks and return exact file paths. In practice, smaller models hallucinate or paraphrase paths. A better approach: **make the phone book a proper index, not prose**. Emit it as structured JSON or SQLite, and give OC a deterministic lookup tool against that index rather than asking the 7B to interpret it. The 7B's KV-cache advantage then applies to answering *semantic* questions ("which files handle memory topology?") while path resolution is exact by construction.

**3. The two-step retrieval loop is underspecified for cross-layer queries.**
Step A ("scan returned blocks") works fine for single-layer queries. But your most valuable use case — "where does a GPU buffer mutation violate a Lean4 bound?" — requires joining across at minimum `#webgpu-buffer`, `#mutation`, and `#proof-bound` tags. The current protocol doesn't define how OC should formulate that join query to the librarian, or how the librarian aggregates across domains. You need an explicit **cross-layer query grammar** in the OC system prompt.

**4. Pass 2 (Reduce) is doing heavy lifting with no guidance.**
"Batch group by functional domain and resolve cross-layer dependencies" is asking a lot of a single prompt with no defined output schema. This pass needs its own output contract — specifically, it should produce **dependency edges**, not just concatenated summaries. Something like: `WebGPU:GPUBuffer[vertex_data] <-> Django:Model[MeshRecord] via endpoint /api/mesh/upload` as a first-class artifact.

---

## Concrete Improvements

**A. Replace the phonebook prose format with a hybrid schema:**
```json
{
  "module": "gpu/mesh_pipeline.ts",
  "intent": "Allocates and binds vertex buffer for mesh rendering",
  "invariants": ["buffer_size <= MAX_VRAM_ALLOC", "bind_group_0 layout matches shader"],
  "tags": ["#webgpu-buffer", "#pipeline", "#memory-topology"],
  "cross_refs": ["api/mesh.py#MeshUploadView", "proofs/mesh_bounds.lean#VertexCountBound"]
}
```
The `cross_refs` field is what makes Pass 2 machine-readable instead of narrative.

**B. Add a staleness layer to the MCP binding:**
The `open-notebook-librarian` MCP server should expose a `check_freshness(file_path, git_hash)` tool alongside `query_librarian`, so OC can validate coordinates before acting on them.

**C. Define the cross-layer query primitive explicitly in OC's system prompt:**
```
CROSS-LAYER QUERY SYNTAX:
To resolve cross-paradigm dependencies, invoke:
query_librarian(anchor_tag="#webgpu-buffer", related_tags=["#proof-bound"], join_type="constraint_violation")
```
This gives the librarian a structured intent rather than a freeform question.

---

The architecture is genuinely clever for a complex multi-paradigm stack. The main gap is that the "phone book" needs to be a proper graph artifact with edges, not a flat indexed document — otherwise the cross-layer value proposition doesn't fully materialize.

Want me to draft a revised schema spec for the phonebook format, or flesh out the Pass 2 reduce contract?

***

Suggest the best student model that we can fit into 8GB VRAM, 24GB RAM. Then based on the expected capabilities of that model and possible cache optimizations, suggest a revised spec and Pass 2.

***

Let me check current model benchmarks and quantization data before making a recommendation.Good, I have what I need. Let me now synthesize the hardware picture with a revised spec.

---

## Model Recommendation: Qwen3.5-9B Q4_K_M

This is the clear winner for your constraints, and the reasons go deeper than just "it fits."

**Why Qwen3.5-9B specifically:**

In real RTX hardware benchmarks, Qwen3.5-9B Q4_K_M delivers 54–58 tokens per second across all context sizes, fits entirely in GPU memory at 32K context at 6.96 GB, and tops the Artificial Analysis Intelligence Index for sub-10B models — a commanding 38% lead over the runner-up on the intelligence index.

That leaves you roughly 1GB of VRAM headroom for KV cache and overhead — tight but workable, and it gets much better with the architecture-specific optimizations below.

**The architectural reason it's especially suited to this role:**

Qwen3.5 uses only 8 of its 32 layers for full attention with KV cache. The other 24 layers use linear attention via Gated Delta Net, with no KV cache at all. The linear/sliding attention layers act as error correction — quantization noise in the few attention layers is absorbed by the surrounding layers.

This is significant for your phonebook use case: the model has structurally lower KV cache pressure than a standard transformer, which means your cached prefix (the entire phonebook) takes up far less VRAM than you'd expect. You can hold a larger active context window than any comparable 9B model.

---

## Cache Optimization Stack

Given your 24GB RAM, you have room to do something most setups can't: push the phonebook permanently into host-memory cache and never recompute it.

**The recommended llama.cpp server invocation:**
```bash
llama-server \
  --model ./models/qwen3.5-9b.Q4_K_M.gguf \
  --ctx-size 32768 \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --cram 8192 \
  --flash-attn \
  --system-prompt-file ./MASTER_PHONEBOOK.md \
  --slot-save-path /var/cache/llama/slots \
  --np 2 \
  --host 0.0.0.0 \
  --port 11434
```

Key flags explained:

- **`--cache-type-k/v q4_0`**: On Qwen3.5-9B, KV cache quantization to q4_0 produces token-level identical output compared to f16, because the 24 linear-attention layers absorb quantization noise. This lets the 33K-token conversation test run at 36.5 tok/s where f16 would OOM. You get lossless compression on this model specifically.

- **`--cram 8192`**: The `--cram` flag allocates host RAM for prompt caching. The rule of thumb is `cram ≈ (number of cached prefixes) × (average prefix size in tokens) × (bytes per token)`. With 24GB RAM and ~7GB used by the model weights on VRAM, you can afford 8GB dedicated to persistent prefix cache — enough to hold your entire phonebook KV state across restarts.

- **`--slot-save-path`**: Without a volume mount for slot save path, your cache disappears between restarts. With it, the phonebook prefix is pre-warmed and never recomputed after the first load.

**Net result:** On the very first query after a cold start, the model ingests the phonebook and warms the cache (~2–4s). Every subsequent query pays zero prompt-processing cost for the phonebook tokens. OC's routing queries hit 54+ t/s from token one.

---

## Revised Phonebook Spec (Exploiting Qwen3.5's Capabilities)

The original spec produced flat prose blocks. Given that Qwen3.5-9B has genuine structured reasoning and 262K native context, you can push the output format harder.

**Replace the prose block format with this:**

```json
{
  "module": "gpu/mesh_pipeline.ts",
  "path": "src/renderer/gpu/mesh_pipeline.ts",
  "intent": "Allocates GPUBuffer for interleaved vertex+normal data and binds BindGroup0 to the render pipeline",
  "layer": "EXECUTION",
  "state_invariants": [
    "buffer_size_bytes <= device.limits.maxBufferSize",
    "bind_group_layout matches WGSL @binding declarations in mesh.wgsl"
  ],
  "contracts": [
    "createMeshBuffer(device: GPUDevice, data: Float32Array): GPUBuffer",
    "bindMeshToPass(pass: GPURenderPassEncoder, group: GPUBindGroup): void"
  ],
  "tags": ["#webgpu-buffer", "#pipeline", "#memory-topology"],
  "cross_refs": [
    {
      "target": "api/mesh.py::MeshUploadView",
      "edge_type": "DATA_SOURCE",
      "contract": "POST /api/mesh/upload → ArrayBuffer → Float32Array"
    },
    {
      "target": "proofs/mesh_bounds.lean::VertexCountBound",
      "edge_type": "CONSTRAINT",
      "contract": "∀ mesh, mesh.vertex_count ≤ MAX_VERTS — guarantees buffer_size_bytes bound"
    }
  ],
  "staleness_key": "sha256:a3f9c1..."
}
```

The `cross_refs` array with typed `edge_type` values (`DATA_SOURCE`, `CONSTRAINT`, `MUTATION_TRIGGER`, `PROOF_DEPENDENCY`) is what makes cross-layer queries resolvable without asking the 7B to reason about it — OC can filter and join on edge type directly.

---

## Revised Pass 2 (Reduce) Contract

The original Pass 2 was vague ("resolve cross-layer dependencies"). Here's a concrete rewrite that gives the 35B model on the ON side an explicit output contract:

```markdown
## PASS 2: CROSS-LAYER DEPENDENCY REDUCER

**Input:** A batch of Module JSON blocks from Pass 1, grouped by functional domain.
**Your task:** Emit ONLY dependency edge objects. Do not re-summarize modules.

**For each cross-layer interaction you detect, emit exactly this structure:**

{
  "edge_id": "E001",
  "edge_type": "<DATA_SOURCE | CONSTRAINT | MUTATION_TRIGGER | PROOF_DEPENDENCY>",
  "source": { "module": "<path>", "symbol": "<function or theorem name>" },
  "target": { "module": "<path>", "symbol": "<function or theorem name>" },
  "invariant_at_boundary": "<The exact property that must hold at this interface>",
  "failure_mode": "<What breaks at runtime or proof-check if this edge is violated>",
  "tags": ["<tags from both sides>"]
}

**Strict rules:**
- If you cannot identify a concrete `invariant_at_boundary`, do NOT emit the edge.
- Edges must be directional: source produces or constrains, target consumes or depends.
- Maximum one sentence per field. No prose explanation outside the JSON structure.
- If a Django model mutation could violate a Lean4 bound, `edge_type` is MUTATION_TRIGGER
  and `failure_mode` must name the specific theorem that would be invalidated.
```

**Why this matters:** The original design asked the 35B to produce narrative about cross-layer dependencies. This version produces a typed edge graph. That graph becomes the second artifact alongside `MASTER_RECON_PHONEBOOK.md` — call it `DEPENDENCY_GRAPH.json` — and OC can query it with a simple filter rather than asking the 7B to infer relationships from prose.

---

## Revised OC Cross-Layer Query Grammar

With the typed edge graph in place, OC's system prompt gains a concrete operator:

```markdown
## CROSS-LAYER QUERY SYNTAX

To find cross-paradigm risks, use:

query_librarian(
  anchor_tag="#webgpu-buffer",
  edge_type="CONSTRAINT",
  related_tags=["#proof-bound"]
)
→ Returns all edges where a Lean4 theorem constrains a WebGPU buffer property.

query_librarian(
  anchor_tag="#mutation",
  edge_type="MUTATION_TRIGGER",
  related_tags=["#lean4-theorem"]
)
→ Returns all Django state mutations that could invalidate a formal invariant.

RULE: After receiving edges, immediately call read_file() on BOTH
source.module AND target.module before writing any code.
Never modify either side of an edge without checking the
`invariant_at_boundary` field first.
```

---

## Summary of Changes

| Component | Original | Revised |
|---|---|---|
| Student model | Unspecified 7B | Qwen3.5-9B Q4_K_M |
| KV cache | Default f16 | q4_0 (lossless on this arch) + `--cram 8GB` persistent |
| Phonebook format | Prose blocks | Typed JSON with `cross_refs` edge array |
| Pass 2 output | Narrative dependency summaries | Typed `DEPENDENCY_GRAPH.json` edge objects |
| OC query interface | Freeform `query_librarian` | Structured filter with `anchor_tag` + `edge_type` |
| Staleness | None | `sha256` key per module block |

The biggest architectural upgrade here is the shift from the phonebook being a *document* the 7B reads to being a *structured index* the 7B routes against. Qwen3.5-9B is capable enough to do semantic tag matching and edge filtering reliably at this parameter count — you don't need it to reason, just to retrieve and route, which plays to its strengths at 54+ t/s.
