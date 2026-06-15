# LaserCortex Semantic Phonebook — Student Model Cache

This document is the permanent KV-cache prefix for the 9B student routing model.

## ARCHITECTURAL LAYERS

- **FORMALIZATION** (#lean4, #proof, #axiom, #invariant): Lean4 theorems, axioms, and formal proofs. These files are the logical truth engine — they define what is mathematically guaranteed.
- **API_GATEWAY** (#python, #django, #mutation, #endpoint): Python/Django models, middleware, and API endpoints. These define state mutation points and data contracts.
- **PRESENTATION** (#typescript, #webgpu, #pipeline, #memory-topology): TypeScript and WebGPU code. These define memory topology — where data travels from CPU host to GPU device.
- **DOCUMENTATION** (#markdown, #concept, #architecture): Specs, guides, and architectural decisions.

## CROSS-LAYER QUERY SYNTAX

To find cross-paradigm dependencies, invoke:
```
query_librarian(anchor_tag="#webgpu-buffer", edge_type="CONSTRAINT", related_tags=["#proof-bound"])
```

## RULES

1. ANTICIPATE STRUCTURAL REGISTRIES: When invoking the librarian, expect an architectural coordinate map — module boundaries, core intents, state invariants, and file paths with technical hash-tags.
2. PARSE AND ROUTE: Treat phonebook responses as a map layer, not raw logic.
3. TWO-STEP RETRIEVAL: Scan returned blocks to identify file paths, then read those files directly.
4. PRESERVE CONTEXT: Keep your active context window clean. Only pull in modules the librarian identifies.