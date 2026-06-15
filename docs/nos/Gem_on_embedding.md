Python, TypeScript, WebGPU, and Lean 4 make for an aggressively sophisticated stack. String matching is definitely doing a massive disservice to a codebase that ranges from high-performance shader logic to formal mathematical proofs.

Standard text embedding models fail heavily on math-heavy codebases because they don't natively comprehend dense symbolic structures, logic tactics (like Lean's `by intro h`), or strict mathematical dependencies.

The top embedding model recommendations are categorized below by how you want to deploy them within your Open Notebook architecture.

---

## Top Embedding Models Compared

| Model Name | Provider / License | Best Suited For | Context Window | Key Advantage |
| --- | --- | --- | --- | --- |
| **zembed-1** | ZeroEntropy / Open-weight | Pure Mathematics & STEM | 32,768 tokens | Dominates STEM & math retrieval benchmarks; out-performs OpenAI's text-3-large by over 35%. |
| **voyage-code-3** | Voyage AI / Commercial API | Codebase & Formal Logic Search | 32,000 tokens | Explicitly trained on text/code/math pairs; supports flexible Matryoshka dimensions and compression. |
| **bge-m3** (or **bge-large-en-v1.5**) | BAAI / Apache 2.0 (ONNX) | Local, Client-side WebGPU Execution | 8,192 tokens | Multi-functional (dense, sparse, multi-vector); converts beautifully to ONNX to run directly on the GPU. |

---

## Detailed Breakdown & Selection Guidance

### 1. The Heavyweight Champ for Pure Math: `zembed-1` (ZeroEntropy)

If your primary RAG hurdle is the **Lean 4** formal logic and heavy mathematical abstractions, this open-weight model is a top choice.

* **Why it fits:** It is specifically optimized to distinguish continuous scientific relevance (e.g., separating a proof for a theorem from a casual mention of that theorem).
* **Context Power:** The 32K token context allows you to embed entire Lean files or large mathematical modules without chunking them into incomprehensible fragments, preserving the overarching logical flow.
* **Deployment:** Best hosted on your Python backend alongside your notebook server.

### 2. The Multi-Language Code Specialist: `voyage-code-3` (Voyage AI)

If your repository is an intricate weave of Python, TypeScript, WGSL shaders, and Lean, `voyage-code-3` is arguably the most practical, production-ready API choice.

* **Why it fits:** Voyage trained this model on trillions of tokens combining syntax trees, code interactions, and mathematical content. It supports over 300 programming languages natively.
* **Matryoshka & Quantization:** It allows you to downscale dimensions (from 2048 down to 512 or 256) and compress to `int8` or `binary` formats with negligible accuracy drops. If your notebook generates millions of code chunks, this will cut your vector database memory footprints drastically.

### 3. The WebGPU-Native Route: `bge-m3` via Transformers.js

Because you explicitly have **TypeScript** and **WebGPU** in your stack, you have a unique opportunity: you can run the embedding generation entirely client-side (zero server costs, zero API latency) directly inside your notebook's execution environment.

* **Why it fits:** Open-source models like `bge-m3` or `bge-large-en-v1.5` have robust ONNX runtime support. You can compile and execute them directly via WebGPU in JavaScript.

---

## Implementation Strategies for Your Stack

### For Local WebGPU Generation (TypeScript)

If you choose to use a local open-source model inside your TypeScript environment, you can lean into `@huggingface/transformers` to target WebGPU execution natively:

```typescript
import { pipeline } from '@huggingface/transformers';

// Allocate a pipeline for local vector extraction accelerated via WebGPU
const embedder = await pipeline('feature-extraction', 'Xenova/bge-large-en-v1.5', {
    device: 'webgpu', // Compiles execution pipelines directly to WebGPU shaders
});

const output = await embedder('theorem my_proof (p q : Prop) : p ∧ q → q ∧ p := by ...', {
    pooling: 'mean',
    normalize: true,
});

// Flat Float32Array containing your math embedding
const embeddingVector = output.data; 

```

### For High-Fidelity Chunking (Lean 4 & Python)

When parsing Lean 4 files for your vector database, avoid simple token-count chunking. Lean 4 functions and theorem environments rely heavily on contextual prerequisites.

* Use an AST-based chunking strategy (via Treesitter or Lean LSP bindings).
* Ensure your chunks isolate whole `theorem`, `lemma`, `def`, or `inductive` structural blocks.
* If using **Voyage Code 3** or **zembed-1**, maximize their 32K context windows by appending related imports or tactic state descriptions directly into the chunk payload before embedding.