Open-Notebook (ON) is **absolutely the right tool** for this orchestration. In fact, its entire architecture is built around the strict conceptual division between "Chat" (conversational RAG exploration) and **"Transformations"** (reducing content to smaller bits of concentrated/dense information).

Because ON exposes a comprehensive REST API and uses SurrealDB, it acts as the perfect environment to store your raw codebase as "Sources" and have your 35B Teacher write its outputs directly as structured "Notes."

Given your highly technical stack—**Django, TypeScript, WebGPU, and Lean4**—a generic summary prompt will fail. The Teacher model needs a rigorous, language-specific parsing blueprint injected into its transformation rules to build a truly high-quality "phone book."

---

## 1. Injecting Prior Knowledge into the Teacher

To make the 35B model build an elite-tier index, you must give it targeted guidance on how to interpret each component of your stack. When writing your ON Content Transformation prompt, define specific extraction rules for each layer:

### The Django Blueprint (The Orchestration Layer)

* **What to extract:** Data models, database relationships, middleware side-effects, API endpoints, and authentication boundaries.
* **Semantic Target:** The Teacher should ignore boilerplate views and focus on documenting **State Mutation Points**.

### The TypeScript / WebGPU Blueprint (The Execution Layer)

* **What to extract:** GPU buffer allocations, Bind Groups, Pipeline layouts, WGSL shader entry points, and worker threads.
* **Semantic Target:** The Teacher must track **Memory Topology**. WebGPU performance lives and dies by resource allocation; the index needs to log exactly where data travels from the CPU host to the GPU device.

### The Lean4 Blueprint (The Formal Mathematical Layer)

* **What to extract:** Axioms, inductive data types, theorem declarations, structural lemmas, and formal invariants.
* **Semantic Target:** Treat Lean4 files as the **Logical Truth Engine**. The Teacher must log the mathematical proof bounds so your remote agent knows exactly what invariants are formally guaranteed by the code.

---

## 2. The Staged Compilation Pipeline in ON

You can use ON’s REST API to automate this into three clean, sequential compilation steps:

```
[Raw Repository]
       │
       ▼ (Step 1: ON Transformation - "Map Manifests")
[File-Level Abstracts (Per file metrics, imports, contracts)]
       │
       ▼ (Step 2: ON Transformation - "Dependency Linker")
[Module Architecture Sheets (Data flows & Type pipelines)]
       │
       ▼ (Step 3: ON Aggregation)
[The Master Phone Book File] ──► (Injected into 7B Client Cache)

```

1. **Step 1: The Raw Extract (Map):** Run a baseline transformation across every individual source file. The Teacher generates a clean, single-page markdown abstract detailing only imports, exports, and core logic signatures.
2. **Step 2: Cross-Module Linking (Reduce):** Run a second transformation that feeds the abstracts of related files together (e.g., feeding the Django API layer abstract alongside the TypeScript WebGPU fetch layer abstract). The Teacher uses this to map the *bridge* between your backend and frontend.
3. **Step 3: Final Index Assembly:** Compile the linked modules into your single master "Phone Book" Note.

---

## 3. Handling Incremental Updates (The "Diff" Problem)

You do not want to recompile your entire phone book every time you change a line of code. Because you use `OpenCode` as your terminal agent, you can write a lightweight file-watcher script or a git hook that handles **Incremental Registry Patching**:

* When a file changes, `OpenCode` detects the modified file path.
* It sends *only* the new file and the old "Phone Book" section to your local ON Teacher instance using a dedicated **"Patch Transformation"** prompt.
* **The Instructions:** *"Compare this updated code file with its existing entry in the Semantic Registry. Rewrite only this module's coordinate section, preserving all other tags and the universal index structure."*
* The script overwrites that specific Note block in ON, and reloads the updated text into your 7B Student's `--prompt-cache`.

By maintaining this loop, your phone book remains completely up-to-date in near-real-time without causing massive, multi-minute processing delays during your coding sessions.

How do you plan to handle the execution of this pipeline—would you prefer it to be an automated process tied to your git workflow, or an explicit command you run inside OpenCode when you finish a major logical chunk?