#!/usr/bin/env python3
"""
Lightweight OpenAI-compatible embedding server using sentence-transformers.

Serves bge-m3 (or any sentence-transformers model) at /v1/embeddings
so Open Notebook can point its credential's endpoint_embedding here.

Usage:
  python3 embedding_server.py [--model BAAI/bge-m3] [--port 8082] [--device cpu]

Endoints:
  GET  /health          — health check
  POST /v1/embeddings   — OpenAI-compatible embedding endpoint
"""

import argparse
import time
import hashlib
import uuid
import threading
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import torch  # for cuda.empty_cache() on GPU

app = FastAPI(title="Embedding Server")

_model = None
_model_name: str = ""
_dim: int = 0
_device: str = "cpu"
_encode_lock = threading.Lock()  # serialize GPU encode calls to avoid OOM


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str = "bge-m3"
    encoding_format: str = "float"


class EmbeddingObject(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingObject]
    model: str
    usage: dict


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": _model_name,
        "dimension": _dim,
        "device": _device,
    }


@app.post("/v1/embeddings")
def create_embeddings(req: EmbeddingRequest):
    global _model, _dim
    if _model is None:
        return {"error": "model not loaded"}, 503

    texts = req.input if isinstance(req.input, list) else [req.input]
    t0 = time.time()
    with _encode_lock:
        embeddings = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        if _device == "cuda":
            torch.cuda.empty_cache()
    elapsed = time.time() - t0

    data = []
    for i, emb in enumerate(embeddings):
        data.append(EmbeddingObject(
            object="embedding",
            embedding=emb.tolist(),
            index=i,
        ))

    total_tokens = sum(len(t.split()) for t in texts)
    return EmbeddingResponse(
        object="list",
        data=data,
        model=_model_name,
        usage={"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    )


def main():
    global _model, _model_name, _dim, _device

    parser = argparse.ArgumentParser(description="Embedding server for Open Notebook")
    parser.add_argument("--model", default="BAAI/bge-m3", help="sentence-transformers model name")
    parser.add_argument("--port", type=int, default=8082, help="Port to listen on")
    parser.add_argument("--device", default="cpu", help="Device (cpu, cuda, mps)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    _model_name = args.model
    _device = args.device

    print(f"Loading model {_model_name} on {_device}...")
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(_model_name, device=_device)
    _dim = _model.get_sentence_embedding_dimension()
    print(f"Model loaded: {_model_name}, dimension={_dim}, device={_device}")
    print(f"Starting server on {args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", timeout_keep_alive=300)


if __name__ == "__main__":
    main()