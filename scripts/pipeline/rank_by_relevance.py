#!/usr/bin/env python3
"""
rank_by_relevance.py — Rank source files by relevance to a research query.

Embeds a preview of each file (path + first N chars of content) via bge-m3,
then ranks by cosine similarity against the query embedding. Runs parallel
requests to the embedding server for speed.

Outputs JSON with full rankings and top-K cutoff for the 35B teacher pass.

Flow:
  1. Walk repo (same exclusions as generate_phonebook.py).
  2. Build "path | content-preview" for each file.
  3. Embed query + all previews via bge-m3 (parallel batches).
  4. Rank by cosine similarity.
  5. Compute top-K from time budget.
  6. Write JSON to stdout (or --output).

Typical time: ~2-3 min for 1400 files with 6 parallel workers.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────
EMBED_URL = "http://localhost:8082/v1/embeddings"
EMBED_MODEL = "bge-m3"
BATCH_SIZE = 50            # items per API call
NUM_WORKERS = 6            # parallel requests (system has 24 CPU cores)
PREVIEW_CHARS = 500        # chars of each file's content to embed (first line + ...
SEC_PER_FILE = 75          # estimated 35B transformation time per file
DEFAULT_TIME_BUDGET = 3 * 3600  # 3 hours
MIN_TOP_K = 1

# Cache: content-addressed embedding cache
CACHE_DIR = Path.home() / ".cache" / "lasercortex"
CACHE_FILE = CACHE_DIR / "embedding_cache.json"
CACHE_VERSION = 1

# Same exclusions as generate_phonebook.py
SKIP_DIRS = {".git", ".lake", "_archive", "__pycache__", "node_modules",
             ".next", "dist", "build", ".pytest_cache", ".mypy_cache",
             ".venv", "venv", ".tox"}
SKIP_PATTERNS = [
    "MASTER_RECON_PHONEBOOK.md",
    "DEPENDENCY_GRAPH.json",
    ".phonebook_cache.json",
]

SUPPORTED_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".lean", ".md",
    ".css", ".html", ".json", ".yaml", ".yml", ".toml",
    ".rs", ".go",
}

# ── Embedding helpers ───────────────────────────────────────────────────────


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Send batch of texts to bge-m3, return list of embedding vectors."""
    if not texts:
        return []
    payload = {"input": texts, "model": EMBED_MODEL}
    try:
        resp = requests.post(EMBED_URL, json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        embeddings = [d["embedding"] for d in sorted(data["data"], key=lambda x: x["index"])]
        return embeddings
    except requests.exceptions.ConnectionError:
        print("\nERROR: Cannot connect to embedding server at", EMBED_URL,
              file=sys.stderr)
        print("  Start it with: python embedding_server.py", file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError) as e:
        print(f"\nERROR: Unexpected embedding API response: {e}", file=sys.stderr)
        sys.exit(1)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Embedding cache (content-addressed) ─────────────────────────────────────


def load_embedding_cache() -> dict[str, list[float]]:
    """Load embedding cache from disk. Returns {sha256: embedding}."""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return {}
        return data.get("entries", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_embedding_cache(cache: dict[str, list[float]]):
    """Save embedding cache to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": CACHE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "num_entries": len(cache),
        "entries": cache,
    }
    # Write atomically via temp file
    tmp = CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    tmp.rename(CACHE_FILE)


def text_sha256(text: str) -> str:
    """Return SHA256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── File walking ────────────────────────────────────────────────────────────


def should_skip(rel_path: str, extra_patterns: list[str] | None = None) -> bool:
    for pat in SKIP_PATTERNS:
        if pat in rel_path:
            return True
    if extra_patterns:
        for pat in extra_patterns:
            if pat in rel_path:
                return True
    return False


def collect_files(root: Path, extra_skip_dirs: set[str] | None = None) -> list[Path]:
    """Collect supported source files, excluding blacklisted dirs & patterns."""
    skip_dirs = SKIP_DIRS | (extra_skip_dirs or set())
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            files.append(fpath)
    return sorted(files)


def resolve_included_paths(root: Path, paths: list[str]) -> list[Path]:
    """Resolve --include-path entries to actual files on disk.

    Accepts absolute paths or paths relative to *root* (the repo).
    Supports glob patterns (``*``, ``**/*.lean``).  Skips nonexistent
    entries with a warning.  Files inside skipped directories (e.g.
    ``.lake/packages/…``) are accepted — the caller injects them
    *after* the walk.
    """
    from glob import glob as globmatch

    result: list[Path] = []
    for p in paths:
        p = p.strip()
        if not p:
            continue
        p_obj = Path(p)
        if p_obj.is_absolute():
            candidates = sorted(globmatch(str(p_obj), recursive=True))
        else:
            candidates = sorted(globmatch(str((root / p)), recursive=True))
        if not candidates:
            print(f"  WARNING: --include-path '{p}' does not match any file",
                  file=sys.stderr)
            continue
        for c in candidates:
            cp = Path(c)
            if cp.is_file():
                result.append(cp)
    # Deduplicate while preserving order
    seen: set[Path] = set()
    deduped = [rp for rp in result if rp not in seen and not seen.add(rp)]
    return sorted(deduped)


def make_preview(fpath: Path) -> str | None:
    """Build 'path | content-preview' string. Returns None for binaries or unreadable."""
    try:
        raw = fpath.read_bytes()[:PREVIEW_CHARS + 1]
        if b'\0' in raw[:128]:
            return None  # binary
        text = raw.decode("utf-8", errors="replace")
        # Get first PREVIEW_CHARS chars, strip leading blank lines
        lines = text.split("\n")
        content = "\n".join(l for l in lines if l.strip())[:PREVIEW_CHARS]
        return content
    except (OSError, PermissionError):
        return None


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Rank source files by relevance to a research query")
    parser.add_argument("root", type=str,
                        help="Root directory to scan")
    parser.add_argument("--query", "-q", type=str, default=None,
                        help="Research query (or pipe via stdin)")
    parser.add_argument("--query-file", type=str, default=None,
                        help="Read query from a text file")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Write JSON output to file (default: stdout)")
    parser.add_argument("--time-budget", type=int, default=DEFAULT_TIME_BUDGET,
                        help=f"Time budget in seconds (default: {DEFAULT_TIME_BUDGET}s = 3h)")
    parser.add_argument("--sec-per-file", type=float, default=SEC_PER_FILE,
                        help=f"Estimated seconds per 35B file (default: {SEC_PER_FILE})")
    parser.add_argument("--top-k", type=int, default=None,
                        help="Override top-K (overrides budget calculation)")
    parser.add_argument("--workers", type=int, default=NUM_WORKERS,
                        help=f"Parallel embed workers (default: {NUM_WORKERS})")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Embeddings per API call (default: {BATCH_SIZE})")
    parser.add_argument("--exclude-dir", type=str, action="append", default=[],
                        help="Directory name to exclude (can be repeated, e.g. --exclude-dir lean4-skills)")
    parser.add_argument("--exclude-pattern", type=str, action="append", default=[],
                        help="Path pattern to exclude (can be repeated)")
    parser.add_argument("--include-path", type=str, action="append", default=[],
                        help="File path to include (bypasses skip-dirs; can be repeated, supports globs)")
    parser.add_argument("--include-list", type=str, default=None,
                        help="File containing paths to include (one per line)")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: Root not found: {root}", file=sys.stderr)
        sys.exit(1)

    # ── Resolve query ──────────────────────────────────────────────────
    query: str | None = args.query
    if not query and args.query_file:
        try:
            query = Path(args.query_file).read_text().strip()
        except OSError as e:
            print(f"ERROR: Cannot read query file: {e}", file=sys.stderr)
            sys.exit(1)
    if not query and not sys.stdin.isatty():
        query = sys.stdin.read().strip()
    if not query:
        print("ERROR: No query provided. Use --query, --query-file, or pipe via stdin.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Query: {query[:150]}...", file=sys.stderr)

    # ── Walk files ─────────────────────────────────────────────────────
    extra_skip_dirs = set(args.exclude_dir)
    skip_patterns = args.exclude_pattern

    print(f"Scanning {root} (exclude dirs: {extra_skip_dirs or 'default'})...",
          file=sys.stderr)
    t0 = time.time()
    all_files = collect_files(root, extra_skip_dirs=extra_skip_dirs)
    # Filter by extension and skip patterns
    filtered: list[Path] = []
    for fpath in all_files:
        rel = str(fpath.relative_to(root))
        if should_skip(rel, skip_patterns):
            continue
        if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
            filtered.append(fpath)
    all_files = filtered
    print(f"  Found {len(all_files)} source files from walk ({time.time() - t0:.1f}s)",
          file=sys.stderr)

    # ── Inject manually included paths (bypass skip-dirs, e.g. .lake/packages/...) ──
    include_paths: list[str] = list(args.include_path)
    if args.include_list:
        try:
            with open(args.include_list) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        include_paths.append(stripped)
        except OSError as e:
            print(f"  WARNING: Cannot read --include-list '{args.include_list}': {e}",
                  file=sys.stderr)
    if include_paths:
        t1 = time.time()
        added = resolve_included_paths(root, include_paths)
        # Filter by extension (but NOT by skip-dirs — user explicitly asked for these)
        added_filtered = [p for p in added if p.suffix.lower() in SUPPORTED_EXTENSIONS]
        # Merge into all_files, dedup, re-sort
        existing = set(all_files)
        for p in added_filtered:
            if p not in existing:
                all_files.append(p)
                existing.add(p)
        all_files.sort()
        print(f"  Added {len(added_filtered)} manually included files ({time.time() - t1:.1f}s)",
              file=sys.stderr)

    print(f"  Total: {len(all_files)} source files", file=sys.stderr)

    # ── Build previews ─────────────────────────────────────────────────
    print("Building file previews (path + first {} chars)...".format(PREVIEW_CHARS),
          file=sys.stderr)
    t0 = time.time()
    previews: list[tuple[Path, str]] = []
    skipped = 0
    for fpath in all_files:
        rel = str(fpath.relative_to(root))
        content = make_preview(fpath)
        if content:
            # Embed "path | content" so the filename context is included
            previews.append((fpath, f"{rel} | {content}"))
        else:
            skipped += 1
    print(f"  {len(previews)} previews ({skipped} skipped) in {time.time() - t0:.1f}s",
          file=sys.stderr)

    # ── Embed query (with cache) ───────────────────────────────────────
    print("Embedding query...", file=sys.stderr)
    t0 = time.time()
    query_cache_key = text_sha256(query)
    embed_cache = load_embedding_cache()
    if query_cache_key in embed_cache:
        query_vec = embed_cache[query_cache_key]
        print(f"  (cached) dim={len(query_vec)} ({time.time() - t0:.1f}s)", file=sys.stderr)
    else:
        query_vec = embed_texts([query])[0]
        embed_cache[query_cache_key] = query_vec
        print(f"  dim={len(query_vec)} ({time.time() - t0:.1f}s)", file=sys.stderr)

    # ── Embed all previews (content-addressed cache + parallel misses) ─
    print("Checking embedding cache...", file=sys.stderr)
    t0 = time.time()

    # Compute SHA256 for each preview text and check cache
    preview_data: list[tuple[Path, str, str]] = []  # (path, preview, sha256)
    all_embeddings: list[list[float] | None] = [None] * len(previews)
    cache_hits = 0
    for i, (fpath, pv_text) in enumerate(previews):
        sha = text_sha256(pv_text)
        preview_data.append((fpath, pv_text, sha))
        if sha in embed_cache:
            all_embeddings[i] = embed_cache[sha]
            cache_hits += 1

    misses = [(i, pv_text) for i, (_, pv_text, sha) in enumerate(preview_data)
              if sha not in embed_cache]
    print(f"  Cache: {cache_hits} hits, {len(misses)} misses in {time.time() - t0:.1f}s",
          file=sys.stderr)

    if misses:
        # Build batches from misses only
        batch_size = args.batch_size
        workers = args.workers
        miss_texts = [(idx, text) for idx, text in misses]
        miss_batches = [miss_texts[i:i + batch_size]
                        for i in range(0, len(miss_texts), batch_size)]
        total_batches = len(miss_batches)
        print(f"Embedding {len(misses)} cache misses (batch={batch_size}, workers={workers}, "
              f"{total_batches} batches)...", file=sys.stderr)

        completed = 0
        errors = 0
        t0 = time.time()
        new_embeddings: dict[int, list[float]] = {}  # preview_index → embedding

        def embed_batch(batch_idx: int, items: list[tuple[int, str]]) -> tuple[int, dict[int, list[float]] | None]:
            texts = [text for _, text in items]
            indices = [idx for idx, _ in items]
            try:
                vecs = embed_texts(texts)
                return batch_idx, dict(zip(indices, vecs))
            except Exception as e:
                print(f"\n  [batch {batch_idx + 1}/{total_batches}] ERROR: {e}",
                      file=sys.stderr)
                return batch_idx, None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(embed_batch, bi, batch): bi
                for bi, batch in enumerate(miss_batches)
            }
            for future in as_completed(futures):
                batch_idx, result = future.result()
                if result is not None:
                    new_embeddings.update(result)
                else:
                    errors += 1
                completed += 1
                elapsed = time.time() - t0
                rate = completed / (elapsed + 0.001)
                eta = (total_batches - completed) / (rate + 0.001)
                print(f"  [{completed}/{total_batches}] {completed / total_batches * 100:.0f}% — "
                      f"{completed * batch_size / (elapsed + 0.001):.0f} files/min — "
                      f"ETA {eta:.0f}s{' ⚠ errors' if errors else ''}",
                      file=sys.stderr, end="\r" if completed < total_batches else "\n")

        # Merge new embeddings into all_embeddings and cache
        for preview_idx, vec in new_embeddings.items():
            all_embeddings[preview_idx] = vec
            # Find the sha for this preview index
            sha = preview_data[preview_idx][2]
            embed_cache[sha] = vec

        # Save updated cache
        save_embedding_cache(embed_cache)
        new_count = len(new_embeddings)
        print(f"  Embedded {new_count} new files, cache now has {len(embed_cache)} entries",
              file=sys.stderr)
    else:
        new_count = 0

    # Filter out failed embeddings (None)
    valid_indices = [i for i, v in enumerate(all_embeddings) if v is not None]
    valid_previews = [(previews[i][0], previews[i][1], all_embeddings[i]) for i in valid_indices]
    failed_count = len(previews) - len(valid_indices)
    print(f"  Total: {len(valid_indices)} embeddings ready ({cache_hits} cached + "
          f"{new_count} new, {failed_count} failed)", file=sys.stderr)

    if not valid_indices:
        print("ERROR: No files were successfully embedded.", file=sys.stderr)
        sys.exit(1)

    # ── Rank by cosine similarity ──────────────────────────────────────
    print("Ranking by cosine similarity...", file=sys.stderr)
    t0 = time.time()
    scored: list[tuple[float, Path, str]] = []
    for fpath, pv, vec in valid_previews:
        score = cosine_similarity(query_vec, vec)
        scored.append((score, fpath, pv))
    scored.sort(key=lambda x: x[0], reverse=True)
    print(f"  Ranked {len(scored)} items in {time.time() - t0:.3f}s", file=sys.stderr)

    # ── Compute top-K ──────────────────────────────────────────────────
    if args.top_k is not None:
        top_k = max(MIN_TOP_K, min(args.top_k, len(scored)))
    else:
        top_k = max(MIN_TOP_K, int(args.time_budget / args.sec_per_file))
        top_k = min(top_k, len(scored))

    top_k_time = top_k * args.sec_per_file
    warnings: list[str] = []
    if top_k_time > DEFAULT_TIME_BUDGET and args.top_k is not None:
        warnings.append(
            f"Top-{top_k} would take {top_k_time // 60}min "
            f"({top_k} files × {args.sec_per_file}s), exceeding budget."
        )
    num_top_k = min(top_k, len(scored))

    # ── Build JSON output ──────────────────────────────────────────────
    ranked_list = []
    for rank, (score, fpath, pv) in enumerate(scored, start=1):
        ranked_list.append({
            "path": str(fpath.relative_to(root)),
            "preview": pv[:120],
            "score": round(score, 6),
            "rank": rank,
        })

    output = {
        "query": query,
        "root": str(root),
        "num_files_found": len(all_files),
        "num_files_embedded": len(valid_indices),
        "skipped_binary": skipped,
        "failed_embeddings": failed_count,
        "exclude_dirs": list(extra_skip_dirs),
        "exclude_patterns": skip_patterns,
        "time_budget_sec": args.time_budget,
        "sec_per_file": args.sec_per_file,
        "top_k": num_top_k,
        "top_k_time_sec": round(num_top_k * args.sec_per_file),
        "top_k_time_human": f"{(num_top_k * args.sec_per_file) // 3600}h "
                            f"{(num_top_k * args.sec_per_file) % 3600 // 60}m",
        "full_pipeline_time_human": f"{len(scored) * args.sec_per_file // 3600}h "
                                    f"{len(scored) * args.sec_per_file % 3600 // 60}m",
        "warnings": warnings,
        "ranked": ranked_list,
        "top_k_paths": [r["path"] for r in ranked_list[:num_top_k]],
    }

    result_json = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(result_json)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(result_json)  # stdout

    # ── Summary to stderr ──────────────────────────────────────────────
    print(file=sys.stderr)
    print(f"═══ Summary ═══", file=sys.stderr)
    print(f"  Files found:       {len(all_files)}", file=sys.stderr)
    print(f"  Files embedded:    {len(valid_indices)}", file=sys.stderr)
    print(f"  Top-K cutoff:      {num_top_k}", file=sys.stderr)
    print(f"  Top-K time est:    {output['top_k_time_human']}", file=sys.stderr)
    print(f"  Full pipeline est: {output['full_pipeline_time_human']}", file=sys.stderr)
    print(f"  Top 10:", file=sys.stderr)
    for r in ranked_list[:10]:
        print(f"    #{r['rank']:4d} {r['score']:.4f}  {r['path']}", file=sys.stderr)
    if warnings:
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
