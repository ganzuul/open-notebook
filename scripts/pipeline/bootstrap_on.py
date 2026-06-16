#!/usr/bin/env python3
"""
Bootstrap Open Notebook with the librarian pipeline configuration.

Idempotent: run multiple times safely. Creates transformations, registers models,
and verifies the deployment is ready.

Usage:
    python3 bootstrap_on.py                    # full bootstrap
    python3 bootstrap_on.py --check             # verify everything is configured
    python3 bootstrap_on.py --reset-models      # delete and recreate models+credentials
"""

import argparse
import json
import sys
import requests

ON_URL = "http://localhost:5055/api"

# --- 9B Student (chat, daytime) ---
CREDENTIAL_NAME_9B = "Local Llama Server (9B, chat)"
CREDENTIAL_PROVIDER = "openai_compatible"
CREDENTIAL_BASE_URL_9B = "http://localhost:11434/v1"
MODEL_NAME_9B = "Qwen_Qwen3.5-9B-Q4_K_M.gguf"
LLAMA_URL_9B = CREDENTIAL_BASE_URL_9B

# --- 35B Teacher (transformations, nighttime) ---
CREDENTIAL_NAME_35B = "35B Teacher (nightly, port 8080)"
CREDENTIAL_BASE_URL_35B = "http://localhost:8080/v1"
MODEL_NAME_35B = "Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf"

# --- Embedding ---
CREDENTIAL_EMBEDDING_URL = "http://localhost:8082/v1"
EMBEDDING_MODEL_NAME = "bge-m3"
EMBEDDING_MODEL_TYPE = "embedding"

TRANSFORMATIONS = [
    {
        "name": "Context-Compiler-V1",
        "title": "Context Compiler V1",
        "description": "Multi-layer semantic extraction: per-file analysis with stack-specific blueprints for Django, TypeScript/WebGPU, Lean4, and Markdown.",
        "prompt": """Role: You are a strict Technical Compiler and Semantic Compressor. Your absolute goal is to ingest raw source code and emit a hyper-dense, structured semantic index entry. Eliminate all conversational filler, chat styling, formatting fluff, and introductory/concluding remarks. Every token you emit must represent structural data or code metadata.

### Extraction Rules per Tech Stack Layer:

1. DJANGO (The Orchestration Layer):
   - Ignore boilerplate views and standard settings imports.
   - Map explicit Data Models, model relationships (FK, M2M), Middleware side-effects, API endpoints, and critical State Mutation Points.
   - Tag with: #django-model, #mutation, #endpoint, or #middleware.

2. TYPESCRIPT / WEBGPU (The Execution Layer):
   - Map GPU buffer allocations, Bind Groups, Pipeline layouts, and WGSL shader entry points.
   - Detail the Host-to-Device Memory Topology (where data copies from RAM to VRAM).
   - Tag with: #webgpu-buffer, #pipeline, #wgsl, #memory-topology, or #async-worker.

3. LEAN4 (The Formal Proof / Mathematical Layer):
   - Extract Axioms, inductive data types, Theorem declarations, and structural Lemmas.
   - Document the formal mathematical bounds and logical invariants guaranteed by the file.
   - Tag with: #lean4-theorem, #axiom, #invariant, or #proof-bound.

4. MARKDOWN (The Documentation Layer):
   - Extract key concepts, terminology definitions, and architectural decisions.
   - Note any cross-references to specific code modules or files.
   - Tag with: #documentation, #concept, #architecture, or #decision.

### Output Format:
For each file, output EXACTLY this structure:

## Module: [name]
**Intent**: One precise sentence describing what this module computes or guarantees.
**Layer**: [FORMALIZATION | API_GATEWAY | PRESENTATION | DOCUMENTATION | SYSTEM]
**Tags**: [space-separated #hashtags matching the stack rules above]
**Contracts**: [list of public API surfaces, theorem names, or function signatures that other modules depend on]
**Cross-refs**: [list of dependencies on other modules and what is consumed — format: module_name -> what_is_used]
**Invariants**: [list of key constraints: memory bounds, proof bounds, state constraints, or type-level guarantees]

Be precise. Use actual names from the code. Do not hallucinate functions or theorems that don't appear in the input.""",
        "apply_default": False,
    },
    {
        "name": "Cross-Layer-Linker",
        "title": "Cross-Layer Dependency Linker",
        "description": "Pass 2 reduce: takes grouped file abstracts and produces typed cross-layer dependency edges showing how modules in different stack layers depend on each other.",
        "prompt": """You are a Dependency Analysis Engine. You will receive a batch of module abstracts from different architectural layers of the same codebase. Your task is to emit ONLY typed cross-layer dependency edges between modules.

### Edge Types:
- DATA_SOURCE: A module provides data that another module consumes (e.g., Django model -> TypeScript fetch)
- CONSTRAINT: A formal proof places a constraint on how data can be used (e.g., Lean4 theorem bounds buffer size)
- MUTATION_TRIGGER: A state change in one layer can violate an invariant in another (e.g., Django mutation -> Lean4 proof invalidation)
- PROOF_DEPENDENCY: A theorem in one module depends on results from another (e.g., Lean4 import chain)
- IMPORT: Direct import dependency within the same layer

### Output:
For each cross-layer dependency you detect, emit exactly this JSON structure:
```json
{
  "edge_id": "E001",
  "edge_type": "<DATA_SOURCE | CONSTRAINT | MUTATION_TRIGGER | PROOF_DEPENDENCY | IMPORT>",
  "source": {"module": "<path>", "symbol": "<function or theorem name>"},
  "target": {"module": "<path>", "symbol": "<function or theorem name>"},
  "invariant_at_boundary": "<The exact property that must hold at this interface>",
  "failure_mode": "<What breaks at runtime or proof-check if this edge is violated>",
  "tags": ["<tags from both sides>"]
}
```

### Strict rules:
- If you cannot identify a concrete `invariant_at_boundary`, do NOT emit the edge.
- Edges must be directional: source produces or constrains, target consumes or depends.
- Maximum one sentence per field. No prose explanation outside the JSON.
- If a Django model mutation could violate a Lean4 bound, `edge_type` is MUTATION_TRIGGER and `failure_mode` must name the specific theorem that would be invalidated.
- Output ONLY valid JSON. No markdown, no commentary.""",
        "apply_default": False,
    },
]

CREDENTIAL_PROVIDER = "openai_compatible"
CREDENTIAL_EMBEDDING_URL = "http://localhost:8082/v1"
MODEL_TYPE = "language"
EMBEDDING_MODEL_NAME = "bge-m3"
EMBEDDING_MODEL_TYPE = "embedding"


def api(method, path, data=None, timeout=30):
    url = f"{ON_URL}{path}"
    try:
        if method == "get":
            r = requests.get(url, timeout=timeout)
        elif method == "post":
            r = requests.post(url, json=data, timeout=timeout)
        elif method == "put":
            r = requests.put(url, json=data, timeout=timeout)
        elif method == "delete":
            r = requests.delete(url, timeout=timeout)
        else:
            raise ValueError(f"Unknown method: {method}")
        return r
    except requests.ConnectionError:
        print(f"  ERROR: Cannot connect to Open Notebook at {ON_URL}")
        print(f"  Make sure ON is running: docker compose up -d")
        sys.exit(1)


def check_on_health():
    r = api("get", "/config")
    if r.status_code != 200:
        print(f"  ERROR: ON returned {r.status_code}")
        return False
    config = r.json()
    print(f"  ON version: {config.get('version', '?')}, DB: {config.get('dbStatus', '?')}")
    return config.get("dbStatus") == "online"


def check_llama():
    try:
        r = requests.get(f"{LLAMA_URL_9B}/models", timeout=5)
        if r.status_code == 200:
            models = r.json().get("data", [])
            model_id = models[0].get("id", "?") if models else "none"
            print(f"  Llama-server (9B) model: {model_id}")
            return True
        return False
    except requests.ConnectionError:
        print(f"  ERROR: Cannot connect to llama-server at {LLAMA_URL_9B}")
        return False


def ensure_transformations():
    existing = {t["name"]: t for t in api("get", "/transformations").json()}
    created = 0
    for t in TRANSFORMATIONS:
        if t["name"] in existing:
            print(f"  Transformation '{t['name']}' already exists: {existing[t['name']]['id']}")
        else:
            r = api("post", "/transformations", data=t)
            if r.status_code == 200:
                print(f"  Created transformation '{t['name']}': {r.json()['id']}")
                created += 1
            else:
                print(f"  ERROR creating '{t['name']}': {r.text[:200]}")
    return created


def ensure_credential(name, base_url, embedding_url=None, reset=False):
    creds = api("get", "/credentials").json()
    existing = None
    for c in creds:
        if c.get("base_url") == base_url:
            existing = c
            break

    if existing and not reset:
        if existing.get("name") != name:
            # Update name if changed
            api("put", f"/credentials/{existing['id']}", data={"name": name})
        if embedding_url and not existing.get("endpoint_embedding"):
            api("put", f"/credentials/{existing['id']}", data={"endpoint_embedding": embedding_url})
        print(f"  Credential: {existing['id']} ({name}, {base_url})")
        return existing["id"]
    elif existing and reset:
        api("delete", f"/credentials/{existing['id']}")
        print(f"  Deleted old credential: {existing['id']}")

    payload = {
        "name": name,
        "provider": CREDENTIAL_PROVIDER,
        "base_url": base_url,
        "api_key": "not-needed",
    }
    if embedding_url:
        payload["endpoint_embedding"] = embedding_url

    r = api("post", "/credentials", data=payload)
    if r.status_code in (200, 201):
        cred = r.json()
        print(f"  Created credential: {cred['id']} ({name}, {base_url})")
        return cred["id"]
    else:
        print(f"  ERROR creating credential: {r.text[:200]}")
        return None


def ensure_model(name, cred_id, reset=False):
    models = api("get", "/models").json()
    existing = None
    for m in models:
        if m.get("name") == name:
            existing = m
            break

    if existing and not reset:
        print(f"  Model already exists: {existing['id']} ({name})")
        return existing["id"]
    elif existing and reset:
        api("delete", f"/models/{existing['id']}")
        print(f"  Deleted old model: {existing['id']}")

    if not cred_id:
        print("  ERROR: No credential ID, cannot create model")
        return None

    r = api("post", "/models", data={
        "name": name,
        "provider": CREDENTIAL_PROVIDER,
        "type": MODEL_TYPE,
        "credential": cred_id,
    })
    if r.status_code == 200:
        model = r.json()
        print(f"  Created model: {model['id']} ({name})")
        return model["id"]
    else:
        print(f"  ERROR creating model: {r.text[:200]}")
        return None


def ensure_embedding_model(cred_id, reset=False):
    models = api("get", "/models").json()
    existing = None
    for m in models:
        if m.get("name") == EMBEDDING_MODEL_NAME:
            existing = m
            break

    if existing and not reset:
        print(f"  Embedding model already exists: {existing['id']} ({existing['name']})")
        return existing["id"]
    elif existing and reset:
        api("delete", f"/models/{existing['id']}")
        print(f"  Deleted old embedding model: {existing['id']}")

    if not cred_id:
        print("  ERROR: No credential ID, cannot create embedding model")
        return None

    r = api("post", "/models", data={
        "name": EMBEDDING_MODEL_NAME,
        "provider": CREDENTIAL_PROVIDER,
        "type": EMBEDDING_MODEL_TYPE,
        "credential": cred_id,
    })
    if r.status_code == 200:
        model = r.json()
        print(f"  Created embedding model: {model['id']} ({EMBEDDING_MODEL_NAME})")
        return model["id"]
    else:
        print(f"  ERROR creating embedding model: {r.text[:200]}")
        return None


def ensure_defaults(chat_model_id, transform_model_id, embedding_model_id):
    defaults = api("get", "/models/defaults").json()
    updates = {}
    if defaults.get("default_chat_model") != chat_model_id:
        updates["default_chat_model"] = chat_model_id
    if defaults.get("default_transformation_model") != transform_model_id:
        updates["default_transformation_model"] = transform_model_id
    if defaults.get("default_embedding_model") != embedding_model_id:
        updates["default_embedding_model"] = embedding_model_id
    if updates:
        r = api("put", "/models/defaults", data=updates)
        if r.status_code == 200:
            print(f"  Updated defaults: {list(updates.keys())}")
        else:
            print(f"  ERROR updating defaults: {r.text[:200]}")
    else:
        print("  Defaults already set correctly")


def check_transformations():
    transforms = api("get", "/transformations").json()
    names = {t["name"] for t in transforms}
    expected = {t["name"] for t in TRANSFORMATIONS}
    missing = expected - names
    if missing:
        print(f"  MISSING transformations: {missing}")
        return False
    print(f"  All {len(expected)} transformations present: {names}")
    return True


def full_status():
    print("=== Open Notebook Librarian Status ===\n")

    print("[1] Open Notebook API:")
    on_ok = check_on_health()

    print("\n[2] Llama-server:")
    llama_ok = check_llama()

    print("\n[3] Transformations:")
    transforms_ok = check_transformations()

    print("\n[4] Credentials:")
    creds = api("get", "/credentials").json()
    cred_9b_ok = cred_35b_ok = False
    for c in creds:
        tag = ""
        if "11434" in c.get("base_url", ""):
            tag = " (9B chat)"
            cred_9b_ok = True
        elif "8080" in c.get("base_url", ""):
            tag = " (35B transformation)"
            cred_35b_ok = True
        print(f"  Credential: {c['id']} ({c['name']}, {c.get('base_url','?')}){tag}")
    if not cred_9b_ok:
        print("  NO credential for 9B on :11434")
    if not cred_35b_ok:
        print("  NO credential for 35B on :8080")

    print("\n[5] Models:")
    models = api("get", "/models").json()
    llm_9b_ok = llm_35b_ok = embed_ok = False
    for m in models:
        tag = "(LLM)" if m.get("type") == "language" else "(embedding)" if m.get("type") == "embedding" else ""
        print(f"  Model: {m['id']} {m['name']} {tag}")
        if m["name"] == MODEL_NAME_9B:
            llm_9b_ok = True
        if m["name"] == MODEL_NAME_35B:
            llm_35b_ok = True
        if m["name"] == EMBEDDING_MODEL_NAME:
            embed_ok = True
    if not llm_9b_ok:
        print(f"  NO model named {MODEL_NAME_9B}")
    if not llm_35b_ok:
        print(f"  NO model named {MODEL_NAME_35B}")
    if not embed_ok:
        print(f"  NO embedding model named {EMBEDDING_MODEL_NAME}")

    print("\n[6] Defaults:")
    defaults = api("get", "/models/defaults").json()
    for k, v in defaults.items():
        if v:
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: (not set)")

    print("\n[7] Notebooks:")
    notebooks = api("get", "/notebooks").json()
    for nb in notebooks:
        print(f"  {nb['name']}: {nb['source_count']} sources, {nb['note_count']} notes")

    print("\n[8] Embedding server:")
    try:
        r = requests.get("http://localhost:8082/health", timeout=3)
        if r.status_code == 200:
            info = r.json()
            print(f"  Running: {info.get('model', '?')} (dim={info.get('dimension', '?')}, {info.get('device', '?')})")
        else:
            print("  ERROR: embedding server returned non-200")
    except requests.ConnectionError:
        print("  NOT RUNNING (start with: just embedding-start)")

    print("\n[9] Librarian server:")
    try:
        r = requests.get("http://localhost:8081/modules", timeout=3)
        if r.status_code == 200:
            modules = r.json()
            print(f"  Running: {len(modules)} modules indexed")
        else:
            print("  ERROR: librarian returned non-200")
    except requests.ConnectionError:
        print("  NOT RUNNING (start with: just librarian)")

    print("\n[10] MCP bridge:")
    mcp_script = "/home/nos/labware/open-notebook/scripts/pipeline/mcp_librarian_server.py"
    import os
    if os.path.exists(mcp_script):
        print(f"  Script present: {mcp_script}")
    else:
        print("  MCP script NOT FOUND")

    all_ok = on_ok and llama_ok and transforms_ok and cred_9b_ok and llm_9b_ok and embed_ok
    print(f"\n{'='*40}")
    print(f"Overall: {'READY' if all_ok else 'NEEDS SETUP'}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Open Notebook librarian pipeline")
    parser.add_argument("--check", action="store_true", help="Check status without making changes")
    parser.add_argument("--reset-models", action="store_true", help="Delete and recreate models/credentials")
    args = parser.parse_args()

    if args.check:
        full_status()
        return

    print("=== Bootstrapping Open Notebook Librarian Pipeline ===\n")

    print("[1] Checking Open Notebook API...")
    if not check_on_health():
        print("  ERROR: ON not available. Start with: docker compose up -d")
        sys.exit(1)

    print("\n[2] Checking llama-server...")
    check_llama()

    print("\n[3] Creating transformations...")
    ensure_transformations()

    print("\n[4] Setting up credentials...")
    print("  [4a] 9B Student (chat, daytime)...")
    cred_id_9b = ensure_credential(CREDENTIAL_NAME_9B, CREDENTIAL_BASE_URL_9B,
                                   embedding_url=CREDENTIAL_EMBEDDING_URL, reset=args.reset_models)
    print("  [4b] 35B Teacher (transformations, nighttime)...")
    cred_id_35b = ensure_credential(CREDENTIAL_NAME_35B, CREDENTIAL_BASE_URL_35B,
                                   reset=args.reset_models)

    print("\n[5] Registering language models...")
    print("  [5a] 9B chat model...")
    model_id_9b = ensure_model(MODEL_NAME_9B, cred_id_9b, reset=args.reset_models)
    print("  [5b] 35B transformation model...")
    model_id_35b = ensure_model(MODEL_NAME_35B, cred_id_35b, reset=args.reset_models)

    print("\n[6] Registering embedding model...")
    embedding_model_id = ensure_embedding_model(cred_id_9b, reset=args.reset_models)

    print("\n[7] Setting model defaults...")
    print("  Chat → 9B, Transformations → 35B, Embedding → bge-m3")
    if model_id_9b and model_id_35b and embedding_model_id:
        ensure_defaults(chat_model_id=model_id_9b, transform_model_id=model_id_35b,
                        embedding_model_id=embedding_model_id)
    else:
        print("  SKIP: one or more model IDs missing")

    print("\n[8] Full status:")
    full_status()


if __name__ == "__main__":
    main()