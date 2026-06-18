#!/usr/bin/env bash
# manage.sh — Model lifecycle management for Dockerized llama.cpp.
#
# Usage:
#   ./manage.sh status                            # model states, VRAM, page cache
#   ./manage.sh swap 9b                           # stop 35B → start 9B (fast)
#   ./manage.sh swap 35b [--query "research..."]  # parallel vmtouch+rank → start 35B
#   ./manage.sh rank [--query "research..."]       # standalone relevance ranking
#   ./manage.sh preload                           # vmtouch 35B only
#   ./manage.sh bake-cache 9b|35b                 # instructions for KV-cache pre-baking
#   ./manage.sh logs [svc]                        # tail logs
#
# Principles:
#   - vmtouch for 35B (21 GB) runs in parallel with relevance ranking.
#   - Both must finish before the model starts.
#   - Docker bind-mounts models read-only — no copies.
#   - If estimated pipeline > 3h, warns.
#
# Pre-flight (35B):
#   1. CUDA kernel cache volume → kernel compile happens ONCE per GPU driver update.
#      Saves ~5 min of CUDA kernel compilation on restarts.
#   2. Parallel vmtouch + ranking: I/O-bound page cache loading runs alongside
#      CPU-bound file relevance ranking. ~3 min total wall time.
#      - vmtouch: ~100s to load 20G into page cache (faults ~6G from disk on start)
#      - ranking: ~5 min first run (784 files), <1s cached
#   3. CUDA kernel cache (69 MB) + page cache (14-17G/20G) combine to make
#      the 35B server ready in ~45-150s (down from ~7 min without caches).

set -euo pipefail
cd "$(dirname "$0")"

# ---- Config ----
MODEL_9B_NAME="Qwen_Qwen3.5-9B-Q4_K_M.gguf"
MODEL_35B_NAME="Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf"
MODELS_DIR="/run/media/nos/games/models"
MODEL_9B="$MODELS_DIR/$MODEL_9B_NAME"
MODEL_35B="$MODELS_DIR/$MODEL_35B_NAME"
PORT_9B=11434
PORT_35B=8080

# Pipeline paths (ON pipeline scripts)
PIPELINE_DIR="/home/nos/labware/open-notebook/scripts/pipeline"
RANKER="$PIPELINE_DIR/rank_by_relevance.py"
RANKING_FILE="/tmp/lasercortex_ranking.json"
REPO_ROOT="/home/nos/labware/LaserCortex"
EMBED_SCRIPT="$REPO_ROOT/scripts/start_embed_server.sh"
LIBRARIAN_SCRIPT="$PIPELINE_DIR/librarian_server.py"
LIBRARIAN_PIDFILE="/tmp/lasercortex-librarian.pid"
LIBRARIAN_PORT=8081

# Docker Compose file for model containers (relative to script dir)
COMPOSE_FILE="docker-compose.models.yml"
export COMPOSE_FILE

# Default research query (used when none is provided via --query)
# This should reflect the current research focus.
RESEARCH_QUERY_DEFAULT="Lean4 formal verification of geometric theorems, Tamari lattice, and binary tree enumeration"

log()   { echo "[$(date '+%H:%M:%S')] $*"; }
warn()  { echo "[$(date '+%H:%M:%S')] WARNING: $*"; }
err()   { echo "[$(date '+%H:%M:%S')] ERROR: $*"; exit 1; }

# ---- Checks ----
check_docker() {
    if ! docker info --format '{{.ServerVersion}}' > /dev/null 2>&1; then
        err "Docker is not running"
    fi
}

check_nvidia() {
    if ! docker info 2>/dev/null | grep -q "nvidia.com/gpu"; then
        err "NVIDIA GPU not available in Docker. Check nvidia-container-toolkit."
    fi
}

check_model() {
    local path="$1"
    [ -f "$path" ] || err "Model not found: $path"
    [ -r "$path" ] || err "Model not readable: $path"
}

check_port() {
    local port="$1"
    if lsof -ti :"$port" > /dev/null 2>&1; then
        err "Port $port is already in use by PID $(lsof -ti :$port)"
    fi
}

check_vram() {
    local needed_mb="${1:-8192}"
    local free_mb
    free_mb=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    if [ -z "$free_mb" ] || [ "$free_mb" -lt "$needed_mb" ]; then
        warn "VRAM: ${free_mb:-0} MiB free, need ~${needed_mb} MiB"
        warn "  Try: kill stuck GPU processes or reset: sudo nvidia-smi --gpu-reset"
        return 1
    fi
}

# ---- Helpers ----
format_duration() {
    local secs=$1
    local hours=$(( secs / 3600 ))
    local mins=$(( (secs % 3600) / 60 ))
    local secs_rem=$(( secs % 60 ))
    if [ "$hours" -gt 0 ]; then
        echo "${hours}h ${mins}m ${secs_rem}s"
    elif [ "$mins" -gt 0 ]; then
        echo "${mins}m ${secs_rem}s"
    else
        echo "${secs_rem}s"
    fi
}

# ---- Actions ----
status() {
    echo "=== Model Server Status ==="
    for svc in model-9b model-35b; do
        local state port name
        case "$svc" in
            model-9b)  port=$PORT_9B; name="9B (Q4, 5.8G)" ;;
            model-35b) port=$PORT_35B; name="35B (Q4, 21G)" ;;
        esac
        if curl -sf "http://localhost:$port/v1/models" > /dev/null 2>&1; then
            echo "  $name on :$port — RUNNING"
        elif docker compose ps --format json "$svc" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('State',''))
except: pass" 2>/dev/null | grep -q "running"; then
            echo "  $name on :$port — STARTING (not yet responsive)"
        else
            echo "  $name on :$port — STOPPED"
        fi
    done

    echo ""
    echo ""
    echo "=== Librarian Index Server ==="
    if [ -f "$LIBRARIAN_PIDFILE" ] && kill -0 "$(cat "$LIBRARIAN_PIDFILE")" 2>/dev/null; then
        echo "  Running on :$LIBRARIAN_PORT (PID $(cat "$LIBRARIAN_PIDFILE"))"
    elif curl -sf "http://localhost:$LIBRARIAN_PORT/modules" > /dev/null 2>&1; then
        echo "  Running on :$LIBRARIAN_PORT (responsive, no PID file)"
    else
        echo "  STOPPED"
    fi

    echo "=== Page Cache ==="
    vmtouch "$MODEL_35B" 2>/dev/null | grep -E "Resident|Pages| percentage" || echo "  (not cached)"
    echo ""
    echo "=== VRAM ==="
    nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv 2>/dev/null
    echo ""
    echo "=== System Memory ==="
    free -h
    echo ""

    # Show latest ranking summary if available
    if [ -f "$RANKING_FILE" ]; then
        echo "=== Latest Ranking Summary ==="
        python3 -c "
import json
with open('$RANKING_FILE') as f:
    d = json.load(f)
print(f'  Query: {d[\"query\"][:100]}...')
print(f'  Files embedded: {d[\"num_files_embedded\"]}')
print(f'  Top-K cutoff:   {d[\"top_k\"]} ({d[\"top_k_time_human\"]})')
print(f'  Full pipeline:  {d[\"full_pipeline_time_human\"]}')
" 2>/dev/null || echo "  (unreadable)"
    fi
}

preload_35b() {
    log "Pre-loading 35B into kernel page cache..."
    log "  Model: $MODEL_35B_NAME (21 GB)"

    local avail_kb
    avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
    local swap_free_kb
    swap_free_kb=$(awk '/SwapFree/{print $2}' /proc/meminfo)
    local model_bytes
    model_bytes=$(stat -c%s "$MODEL_35B" 2>/dev/null || echo 0)

    log "  Memory: available $(numfmt --to=iec $((avail_kb * 1024)))"
    log "  Swap:   free $(numfmt --to=iec $((swap_free_kb * 1024)))"
    log "  Model:  $(numfmt --to=iec $model_bytes)"

    local total_avail=$(( avail_kb * 1024 + swap_free_kb * 1024 ))
    if [ "$total_avail" -lt "$model_bytes" ]; then
        warn "Available RAM + swap ($(( total_avail / 1048576 )) MiB) < model size ($(( model_bytes / 1048576 )) MiB)"
        warn "  vmtouch may fail. Proceeding anyway (mmap can page fault from disk)."
    fi

    # -t = touch pages (load into page cache, no mlock — we don't have 21G free RAM)
    vmtouch -t "$MODEL_35B"
    log "  Pre-load complete."
    vmtouch "$MODEL_35B" 2>/dev/null | grep -E "Resident| percentage"
}

run_ranking() {
    local query="$1"
    log "Ranking files by relevance to research query..."
    log "  Query: ${query:0:120}..."
    log "  This runs in parallel with vmtouch."

    # If .manual_pipeline_queue exists, pass as --include-list
    local include_list_arg=()
    if [ -f "$REPO_ROOT/.manual_pipeline_queue" ]; then
        include_list_arg=("--include-list" "$REPO_ROOT/.manual_pipeline_queue")
        log "  Include list: $REPO_ROOT/.manual_pipeline_queue ($(wc -l < "$REPO_ROOT/.manual_pipeline_queue") entries)"
    fi

    local ranker_args=(
        "$REPO_ROOT"
        "--query" "$query"
        "--exclude-dir" "direct_infra_experiment"
        "--exclude-dir" "democh_20260120_134822.normcode-portable"
        "${include_list_arg[@]}"
        "--output" "$RANKING_FILE"
        "--workers" "6"
        "--batch-size" "50"
    )

    python3 "$RANKER" "${ranker_args[@]}"
    local rc=$?
    if [ $rc -ne 0 ]; then
        warn "Ranking failed (exit $rc). Proceeding without ranking."
        return 1
    fi

    log "Ranking complete. Saved to $RANKING_FILE"
    return 0
}

read_pipeline_estimate() {
    # Read the ranking file and print estimated pipeline time in seconds
    if [ ! -f "$RANKING_FILE" ]; then
        echo "null"
        return
    fi
    python3 -c "
import json
with open('$RANKING_FILE') as f:
    d = json.load(f)
top_k = d.get('top_k', 0)
sec_per_file = d.get('sec_per_file', 75)
print(top_k * sec_per_file)
" 2>/dev/null || echo "null"
}

swap() {
    local target="${1:-}"
    shift 2>/dev/null || true

    # Parse --query if present (for 35b)
    local query="$RESEARCH_QUERY_DEFAULT"
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) query="$2"; shift 2 ;;
            --query=*) query="${1#*=}"; shift ;;
            *) shift ;;
        esac
    done

    case "$target" in
        9b)
            log "Swapping 35B → 9B..."
            check_docker
            log "  Starting 9B (Q4, 5.8 GB)..."
            docker compose stop model-35b 2>/dev/null || true
            docker compose up -d model-9b
            log "  9B starting — will be ready in ~10-30s"
            ;;

        35b)
            log "Swapping 9B → 35B..."
            log "═══ Pre-flight ═══"
            check_docker
            check_nvidia
            check_model "$MODEL_35B"
            check_port $PORT_35B

            # ── Parallel pre-flight: vmtouch + ranking ──────────────
            # vmtouch (I/O bound, ~100s for 20G on SATA SSD)
            # ranking (CPU bound, ~5 min first run, <1s cached)
            # Run both concurrently — total wall time ≈ max(vmtouch, ranking)

            log "  Phase 1: Starting parallel pre-flight..."
            log "    • vmtouch 35B into page cache (background)"
            log "    • rank files by relevance to query (background)"

            RANKING_PID=""
            VMTOUCH_PID=""

    # Start ranking in background
    if [ -f "$RANKER" ]; then
        run_ranking "$query" &
        RANKING_PID=$!
    else
        warn "Ranker not found at $RANKER — skipping relevance ranking."
    fi

            # Start vmtouch in this shell (foreground — ~100s for 20G on SATA SSD)
            preload_35b
            VMTOUCH_RC=$?

            # Wait for ranking if it's still running
            if [ -n "$RANKING_PID" ]; then
                log "  Waiting for ranking to finish..."
                wait "$RANKING_PID" 2>/dev/null
                RANKING_RC=$?
                if [ $RANKING_RC -ne 0 ]; then
                    warn "Ranking had issues (exit $RANKING_RC). Continuing without ranking data."
                fi
            fi

            log "  Pre-flight complete."

            # ── Estimate pipeline time ─────────────────────────────
            log ""
            log "═══ Pipeline Estimate ═══"
            local top_k_time=$(read_pipeline_estimate)
            if [ "$top_k_time" != "null" ] && [ -n "$top_k_time" ]; then
                log "  Top-K pipeline estimate: $(format_duration $top_k_time)"

                # Also show full pipeline estimate from ranking file
                python3 -c "
import json
with open('$RANKING_FILE') as f:
    d = json.load(f)
tot = d.get('num_files_embedded', 0) * d.get('sec_per_file', 75)
top = d.get('top_k', 0) * d.get('sec_per_file', 75)
print(f'  Files: {d[\"num_files_embedded\"]} embedded, top-{d[\"top_k\"]} selected')
print(f'  Full pipeline:  {d.get(\"full_pipeline_time_human\", \"?\")}')
print(f'  Top-K pipeline: {d.get(\"top_k_time_human\", \"?\")}')
print(f'  Max score: {d[\"ranked\"][0][\"score\"]:.3f}  {d[\"ranked\"][0][\"path\"]}')
" 2>/dev/null || true

                if [ "$top_k_time" -gt 10800 ]; then  # 3 hours
                    warn "  Pipeline estimated at $(format_duration $top_k_time) — that's > 3h!"
                    warn "  Consider: reduce --time-budget or use --mode fast / --mode incremental"
                fi
            else
                log "  (no ranking data — cannot estimate)"
                # Fallback to rough estimate
                local files=$([ -f "$RANKING_FILE" ] && python3 -c "import json; print(json.load(open('$RANKING_FILE')).get('num_files_embedded',1200))" || echo 1200)
                local rough_est=$(( files * 75 ))
                log "  Rough estimate (all files): $(format_duration $rough_est)"
            fi

            # ── Free GPU: stop embed server if running ──────────────
            # bge-m3 (~3.3 GiB) + 35B (~8 GB) cannot coexist on 8 GB VRAM.
            # Ranking is already complete at this point, so embed server
            # is no longer needed.
            if [ -f /tmp/lasercortex-embed.pid ]; then
                log ""
                log "═══ Freeing GPU ═══"
                log "  Stopping embedding server to free VRAM for 35B..."
                "$EMBED_SCRIPT" kill 2>/dev/null || true
                log "  Embed server stopped."
            fi

            # ── Start model ─────────────────────────────────────────
            log ""
            log "═══ Starting Model ═══"
            log "  Starting 35B (Q4, 21 GB)..."
            docker compose stop model-9b 2>/dev/null || true
            docker compose up -d model-35b
            log "  35B starting — CUDA kernel cache + page cache should make this fast."
            log "  Watch: ./manage.sh logs model-35b"
            log "  Ranking file: $RANKING_FILE (read by pipeline scripts)"
            ;;

        *)
            err "Usage: $0 swap {9b|35b} [--query \"...\"]"
            ;;
    esac
}

case "${1:-help}" in
    status)
        status
        ;;
    swap)
        shift  # remove "swap" from $@, leaving "35b --query ..."
        swap "$@"
        ;;
    rank)
        # Standalone ranking (useful for testing / inspection)
        query="$RESEARCH_QUERY_DEFAULT"
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                --query) query="$2"; shift 2 ;;
                --query=*) query="${1#*=}"; shift ;;
                *) shift ;;
            esac
        done
        [ -f "$RANKER" ] || err "Ranker not found: $RANKER"
        check_docker  # embedding server runs as Docker container, just check it's available
        run_ranking "$query"
        ;;
    preload)
        check_model "$MODEL_35B"
        preload_35b
        ;;
    embed)
        # Manage the embedding server (bge-m3 on :8082).
        # Device can be cpu (default) or gpu (cuda).
        action="${2:-status}"
        shift 2 2>/dev/null || shift
        [ -x "$EMBED_SCRIPT" ] || err "Embed script not found: $EMBED_SCRIPT"
        case "$action" in
            cpu)
                log "Starting embedding server on CPU..."
                "$EMBED_SCRIPT" start "$@"
                ;;
            gpu)
                log "Starting embedding server on GPU..."
                # Check if a model container is running — embed + model won't
                # fit on 8 GB VRAM.
                if docker compose ps --status running 2>/dev/null | grep -q model-; then
                    warn "A model container is running — bge-m3 (~3.3 GiB) may not share GPU."
                    warn "  Stop it first: $0 unload"
                fi
                check_vram 2048 || warn "Low VRAM — GPU embedding may fail"
                "$EMBED_SCRIPT" start --device cuda "$@"
                ;;
            stop)
                "$EMBED_SCRIPT" kill
                ;;
            status)
                "$EMBED_SCRIPT" status
                ;;
            restart)
                log "Restarting embedding server..."
                "$EMBED_SCRIPT" kill 2>/dev/null || true
                sleep 1
                "$EMBED_SCRIPT" start "$@"
                ;;
            *)
                err "Usage: $0 embed {cpu|gpu|stop|status|restart} [args...]"
                ;;
        esac
        ;;
    unload)
        # Stop a model without starting another.
        action="${2:-}"
        if [ -z "$action" ]; then
            # Stop whatever is running
            docker compose stop 2>/dev/null || true
            log "All models stopped"
        else
            case "$action" in
                9b)
                    docker compose stop model-9b
                    log "9B stopped"
                    ;;
                35b)
                    docker compose stop model-35b
                    log "35B stopped"
                    ;;
                *)
                    err "Usage: $0 unload {9b|35b}"
                    ;;
            esac
        fi
        ;;
    bake-cache)
        local target="${2:-}"
        case "$target" in
            9b)
                echo "Pre-bake KV-cache for 9B:"
                echo "  The cache is written automatically after the first"
                echo "  request with --prompt-cache-all. To force it:"
                echo "    curl -X POST http://localhost:$PORT_9B/v1/chat/completions \\"
                echo "      -d '{\"messages\":[{\"role\":\"system\",\"content\":\"$(head -c 200 /path/to/system_prompt.txt)\"}],\"max_tokens\":1}'"
                echo "  Then check: docker compose exec model-9b ls -lh /cache/"
                ;;
            35b)
                echo "Pre-bake KV-cache for 35B:"
                echo "  Same as 9B but on port $PORT_35B."
                echo "  The prompt-cache volume persists across restarts."
                ;;
            *)
                err "Usage: $0 bake-cache {9b|35b}"
                ;;
        esac
        ;;
    logs)
        docker compose logs -f --tail=50 "${2:-}" 2>/dev/null || docker compose logs --tail=50 "${2:-}"
        ;;
    librarian)
        # Manage the librarian index server (FastAPI on :8081).
        action="${2:-status}"
        shift 2 2>/dev/null || shift
        case "$action" in
            start)
                if [ -f "$LIBRARIAN_PIDFILE" ] && kill -0 "$(cat "$LIBRARIAN_PIDFILE")" 2>/dev/null; then
                    log "Librarian server already running (PID $(cat "$LIBRARIAN_PIDFILE"))"
                    exit 0
                fi
                [ -f "$LIBRARIAN_SCRIPT" ] || err "Librarian script not found: $LIBRARIAN_SCRIPT"
                log "Starting librarian index server on :$LIBRARIAN_PORT..."
                nohup python3 -m uvicorn librarian_server:app \
                    --host 0.0.0.0 --port "$LIBRARIAN_PORT" \
                    --app-dir "$PIPELINE_DIR" \
                    --log-level info \
                    > /tmp/lasercortex-librarian.log 2>&1 &
                echo $! > "$LIBRARIAN_PIDFILE"
                # Wait for it to be ready
                for i in $(seq 1 10); do
                    if curl -sf "http://localhost:$LIBRARIAN_PORT/modules" > /dev/null 2>&1; then
                        log "Librarian server ready on :$LIBRARIAN_PORT"
                        break
                    fi
                    sleep 1
                done
                ;;
            stop)
                if [ -f "$LIBRARIAN_PIDFILE" ]; then
                    PID=$(cat "$LIBRARIAN_PIDFILE")
                    log "Stopping librarian server (PID $PID)..."
                    kill "$PID" 2>/dev/null || true
                    sleep 1
                    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
                    rm -f "$LIBRARIAN_PIDFILE"
                    log "Stopped"
                else
                    # Try to find and kill by port
                    PID=$(lsof -ti :$LIBRARIAN_PORT 2>/dev/null || true)
                    if [ -n "$PID" ]; then
                        log "Stopping librarian server on port :$LIBRARIAN_PORT (PID $PID)..."
                        kill "$PID" 2>/dev/null || true
                        log "Stopped"
                    else
                        log "Librarian server not running"
                    fi
                fi
                ;;
            status)
                if [ -f "$LIBRARIAN_PIDFILE" ] && kill -0 "$(cat "$LIBRARIAN_PIDFILE")" 2>/dev/null; then
                    echo "Running on :$LIBRARIAN_PORT (PID $(cat "$LIBRARIAN_PIDFILE"))"
                elif lsof -ti :$LIBRARIAN_PORT > /dev/null 2>&1; then
                    echo "Running on :$LIBRARIAN_PORT (PID $(lsof -ti :$LIBRARIAN_PORT))"
                else
                    echo "Not running"
                fi
                ;;
            restart)
                log "Restarting librarian server..."
                "$0" librarian stop
                sleep 1
                "$0" librarian start
                ;;
            logs)
                tail -30 /tmp/lasercortex-librarian.log 2>/dev/null || echo "No log file found"
                ;;
            *)
                err "Usage: $0 librarian {start|stop|status|restart|logs}"
                ;;
        esac
        ;;
    pipeline)
        # Manage the 35B Deep Analysis pipeline as a background batch job.
        # Starts 35B model if needed, runs generate_phonebook on outstanding
        # files, then runs the Cross-Layer-Linker.
        action="${2:-status}"
        shift 2 2>/dev/null || shift
        PIPELINE_SCRIPT="$REPO_ROOT/scripts/run_pipeline_bg.sh"
        PIPELINE_LOGFILE="/tmp/lasercortex-pipeline-$(date +%Y%m%d).log"
        case "$action" in
            start)
                # Parse --top-k from remaining args before doing anything else.
                # (Can't use `local` outside a function in bash, so we use
                #  the global namespace carefully.)
                top_k=""
                declare -a remaining=()
                while [ $# -gt 0 ]; do
                    case "$1" in
                        --top-k) top_k="$2"; shift 2 ;;
                        --top-k=*) top_k="${1#*=}"; shift ;;
                        *) remaining+=("$1"); shift ;;
                    esac
                done
                set -- "${remaining[@]}"
                unset remaining

                # ── Ensure 35B model is running ──────────────────────
                if ! curl -sf "http://localhost:$PORT_35B/v1/models" > /dev/null 2>&1; then
                    log "35B not running — swapping now..."
                    "$0" swap 35b
                else
                    log "35B already running on :$PORT_35B"
                fi

                # ── Ensure librarian server is running ───────────────
                if ! curl -sf "http://localhost:$LIBRARIAN_PORT/modules" > /dev/null 2>&1; then
                    log "Librarian server not running — starting..."
                    "$0" librarian start
                else
                    log "Librarian server already running on :$LIBRARIAN_PORT"
                fi

                # ── Launch pipeline job ──────────────────────────────
                log "Launching 35B pipeline in background..."
                [ -x "$PIPELINE_SCRIPT" ] || err "Pipeline script not found: $PIPELINE_SCRIPT"

                # Pass TOP_K through environment so run_pipeline_bg.sh picks it up
                if [ -n "${top_k:-}" ]; then
                    log "  TOP_K=$top_k (capped at $top_k files per run)"
                    TOP_K="$top_k" "$PIPELINE_SCRIPT" start
                else
                    log "  TOP_K=unset (processing all remaining files)"
                    "$PIPELINE_SCRIPT" start
                fi

                pid=""
                [ -f "/tmp/lasercortex-pipeline.pid" ] && pid=$(cat "/tmp/lasercortex-pipeline.pid")
                log "Pipeline job launched${pid:+ (PID $pid)}"
                log "  Log: $PIPELINE_LOGFILE"
                log "  Watch: $0 pipeline logs"
                log "  Stop:  $0 pipeline kill"
                ;;
            status)
                "$PIPELINE_SCRIPT" status 2>/dev/null || echo "Not running"
                # Show how many files remain
                if [ -f "$REPO_ROOT/.phonebook_cache.json" ]; then
                    python3 -c "
import json
with open('$REPO_ROOT/.phonebook_cache.json') as f:
    cache = json.load(f)
total = len(cache)
done = sum(1 for v in cache.values() if v.get('transform_sha'))
remaining = total - done
est_hours = remaining * 75 / 3600
print(f'  35B Deep Analysis: {done}/{total} files ({remaining} remaining, ~{est_hours:.1f}h at 75s/file)')
" 2>/dev/null || true
                fi
                ;;
            logs)
                "$PIPELINE_SCRIPT" logs 2>/dev/null || echo "No pipeline log yet"
                ;;
            kill)
                log "Stopping pipeline job..."
                "$PIPELINE_SCRIPT" kill
                # Clean up the lockfile if it was left behind
                rm -f /tmp/lasercortex-pipeline.lock
                log "Stopped"
                ;;
            estimate)
                # Show estimated time for remaining files
                python3 -c "
import json
with open('$REPO_ROOT/.phonebook_cache.json') as f:
    cache = json.load(f)
total = len(cache)
done = sum(1 for v in cache.values() if v.get('transform_sha'))
remaining = total - done
est_secs = remaining * 75
est_hours = est_secs / 3600
est_days = est_hours / 24
print(f'Remaining: {remaining} files (out of {total})')
print(f'Estimated: {est_secs}s = {est_hours:.1f}h = {est_days:.2f}d at 75s/file')
print(f'With --top-k 144: 144 × 75s = {144*75}s = {144*75/3600:.1f}h per run')
if remaining > 0:
    runs_needed = (remaining + 143) // 144
    print(f'Runs needed with --top-k: ~{runs_needed}')
    print(f'Suggest: {remaining // 144} batches of --top-k 144, then 1 final run without cap')
" 2>/dev/null || echo "Cannot read cache file"
                ;;
            *)
                err "Usage: $0 pipeline {start|status|logs|kill|estimate}"
                ;;
        esac
        ;;
    *)
        echo "Usage: $0 {status|swap|rank|embed|unload|preload|bake-cache|librarian|logs}"
        echo ""
        echo "  status                           — model states, page cache, VRAM, librarian"
        echo "  swap 9b                          — stop 35B, start 9B (fast)"
        echo "  swap 35b [--query \"...\"]        — vmtouch + rank in parallel, then start 35B"
        echo "  rank [--query \"...\"]            — standalone relevance ranking (no model swap)"
        echo "  embed cpu|gpu|stop|status        — manage bge-m3 embedding server on :8082"
        echo "  librarian start|stop|status      — manage librarian index server on :8081"
        echo "  unload [9b|35b]                  — stop a model (default: both)"
        echo "  preload                          — vmtouch 35B into page cache only"
        echo "  pipeline start [--top-k N]       — start 35B batch job (all files if no --top-k)"
        echo "  pipeline status                  — check pipeline job + remaining files"
        echo "  pipeline logs                    — tail pipeline log"
        echo "  pipeline kill                    — stop pipeline gracefully"
        echo "  pipeline estimate                — estimate time for remaining files"
        echo "  bake-cache 9b|35b                — instructions for KV-cache pre-baking"
        echo "  logs [svc]                       — tail logs"
        ;;
esac
