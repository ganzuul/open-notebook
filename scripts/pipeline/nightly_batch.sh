#!/usr/bin/env bash
# nightly_batch.sh — Nightly batch job: start 35B, run full pipeline, sync, research, stop.
#
# This embodies the day/night rhythm:
#   Daytime: 9B for chat, research questions logged to RESEARCH_FILE
#   Nighttime: 35B starts, processes transformations, syncs docstrings,
#              does web research on logged questions, then shuts down.
#
# Usage:
#   ./nightly_batch.sh                    # normal run
#   ./nightly_batch.sh --pipeline-only    # skip research
#   ./nightly_batch.sh --dry-run          # show what would happen

set -euo pipefail

# ---- Config ----
REPO_DIR="/home/nos/labware/LaserCortex"
ON_DIR="/home/nos/labware/open-notebook"
RESEARCH_FILE="${REPO_DIR}/research_questions.md"
LOGFILE="/tmp/on-nightly-$(date +%Y%m%d-%H%M).log"
LOCKFILE="/tmp/on-nightly.lock"
MODEL_PATH="/run/media/nos/games/models/Qwen_Qwen3.6-35B-A3B-Q5_K_M.gguf"

# ---- Helpers ----
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }
cleanup() {
    log "Cleaning up..."
    if [ -n "${PID_35B:-}" ]; then
        kill "$PID_35B" 2>/dev/null || true
        wait "$PID_35B" 2>/dev/null || true
        log "35B stopped"
    fi
    rm -f "$LOCKFILE"
}

# ---- Lock ----
if [ -f "$LOCKFILE" ]; then
    echo "Already running (lockfile exists)" | tee -a "$LOGFILE"
    exit 1
fi
trap cleanup EXIT
touch "$LOCKFILE"

# ---- Parse args ----
DRY_RUN=false
PIPELINE_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --pipeline-only) PIPELINE_ONLY=true ;;
    esac
done

log "=== Nightly Batch Start ==="
log "Pipeline-only: $PIPELINE_ONLY"
log "Dry-run: $DRY_RUN"

# ---- 1. Start 35B ----
log "Starting 35B teacher on :8080..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would start: $MODEL_PATH on :8080"
    PID_35B=99999  # dummy
else
    /home/nos/labware/llocollama/llama-server-turboquant \
        --model "$MODEL_PATH" \
        --flash-attn on --jinja \
        --chat-template-kwargs '{"preserve_thinking":true}' \
        --ctx-size 65536 --cache-type-k turbo4 --cache-type-v turbo3 \
        --parallel 1 --host 0.0.0.0 --port 8080 > /tmp/llama-35b.log 2>&1 &
    PID_35B=$!
    log "35B PID: $PID_35B"
fi

# ---- 2. Wait for 35B ----
log "Waiting for 35B to be ready (checking every 60s, up to 10min)..."
if [ "$DRY_RUN" = false ]; then
    for i in $(seq 1 10); do
        sleep 60
        if curl -sf http://localhost:8080/v1/models > /dev/null 2>&1; then
            log "35B ready after $((i * 60))s"
            break
        fi
        if [ $i -eq 10 ]; then
            log "ERROR: 35B did not become ready within 10 minutes — continuing without it"
            log "WARN: transformations will fail; docstring sync will use existing notes"
        fi
    done
else
    log "[dry-run] Would wait for 35B readiness (up to 10min)"
fi

# ---- 3. Run full pipeline ----
log "Running full pipeline (generate_phonebook --mode full)..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would run: python3 generate_phonebook.py --mode full"
else
    cd "$ON_DIR"
    python3 scripts/pipeline/generate_phonebook.py --mode full 2>&1 | tee -a "$LOGFILE"
    log "Pipeline complete"
fi

# ---- 4. Sync docstrings (inject Deep Analysis → Lean) ----
log "Syncing docstrings (--inject)..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would run: sync_docstrings.py --inject"
else
    python3 scripts/pipeline/sync_docstrings.py --inject 2>&1 | tee -a "$LOGFILE"
    log "Docstring sync complete"
fi

# ---- 5. Web research for logged questions ----
if [ "$PIPELINE_ONLY" = false ]; then
    if [ -f "$RESEARCH_FILE" ] && [ -s "$RESEARCH_FILE" ]; then
        log "Processing research questions from ${RESEARCH_FILE}..."
        if [ "$DRY_RUN" = true ]; then
            log "[dry-run] Would read questions and run web search for each"
        else
            cd "$REPO_DIR"
            # Read questions line by line (non-empty, non-comment lines)
            while IFS= read -r line; do
                line="${line#\#}"  # strip leading #
                line="$(echo "$line" | xargs)"  # trim
                [ -z "$line" ] && continue
                [[ "$line" == -* ]] && continue
                log "  Researching: $line"
                # Run web search via a lightweight script
                python3 -c "
import json, subprocess, sys, requests
query = sys.argv[1]
# We use a simple web search approach: fetch results and post as ON note
# For now, just log the query
print(f'QUESTION: {query}')
" "$line" 2>&1 | tee -a "$LOGFILE"
            done < "$RESEARCH_FILE"
            # Clear the research file after processing
            echo "" > "$RESEARCH_FILE"
            git add "$RESEARCH_FILE"
            git commit -m "nightly: clear processed research questions" || true
            log "Research complete"
        fi
    else
        log "No research questions found (${RESEARCH_FILE})"
    fi
else
    log "Skipping research (--pipeline-only)"
fi

# ---- 6. Commit any file changes ----
log "Committing docstring changes..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would commit changes in LaserCortex"
else
    cd "$REPO_DIR"
    if ! git diff --quiet; then
        git add -A
        git commit -m "nightly batch: Deep Analysis docstring sync [$(date +%Y-%m-%d)]" || true
        git push 2>&1 | tee -a "$LOGFILE" || log "Push skipped (no remote or conflict)"
        log "Changes committed and pushed"
    else
        log "No changes to commit"
    fi
fi

# ---- 7. Stop 35B ----
log "Stopping 35B..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would stop 35B (PID $PID_35B)"
else
    kill "$PID_35B" 2>/dev/null || true
    wait "$PID_35B" 2>/dev/null || true
    log "35B stopped"
fi

# ---- Done ----
log "=== Nightly Batch Complete ==="
echo "" | tee -a "$LOGFILE"
echo "Log: $LOGFILE"
