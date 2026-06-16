#!/usr/bin/env bash
# nightly_batch.sh — Nightly batch: swap 9B→35B, run full pipeline, leave 35B loaded.
#
# Principle: never unload a model unless another is being loaded or manually requested.
#   1. Stop 9B (if running) → start 35B (if not already running)
#   2. Run full pipeline
#   3. DONE — 35B stays in VRAM for subsequent use
#   4. To swap back to 9B, run: morning_batch.sh
#
# Usage:
#   ./nightly_batch.sh                    # normal run
#   ./nightly_batch.sh --pipeline-only    # skip research
#   ./nightly_batch.sh --dry-run          # show what would happen
#   ./nightly_batch.sh --force            # re-run pipeline even if 35B was already loaded

set -euo pipefail

# ---- Config ----
REPO_DIR="/home/nos/labware/LaserCortex"
ON_DIR="/home/nos/labware/open-notebook"
LLOCO_DIR="/home/nos/labware/llocollama"
RESEARCH_FILE="${REPO_DIR}/research_questions.md"
LOGFILE="/tmp/on-nightly-$(date +%Y%m%d-%H%M).log"
LOCKFILE="/tmp/on-nightly.lock"

MODEL_35B_PATH="/run/media/nos/games/models/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf"
MODEL_9B_PATH="/run/media/nos/games/models/Qwen_Qwen3.5-9B-Q4_K_M.gguf"
PORT_35B=8080
PORT_9B=11434

# ---- Helpers ----
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }
cleanup() {
    log "Cleaning up..."
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
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --pipeline-only) PIPELINE_ONLY=true ;;
        --force) FORCE=true ;;
    esac
done

log "=== Nightly Batch Start ==="
log "Pipeline-only: $PIPELINE_ONLY | Dry-run: $DRY_RUN | Force: $FORCE"

# ---- 1. Ensure 35B is running ----
log "Checking 35B on :$PORT_35B..."
if curl -sf "http://localhost:$PORT_35B/v1/models" > /dev/null 2>&1; then
    log "35B already loaded — skipping model swap"
    if [ "$FORCE" = false ]; then
        log "Use --force to re-run the pipeline even if 35B was already loaded"
    fi
else
    log "35B not running — swapping 9B→35B via manage.sh"
    if [ "$DRY_RUN" = false ]; then
        # Read the research query from research_questions.md (first non-empty, non-comment line)
        RESEARCH_QUERY=$(grep -v '^#' "$RESEARCH_FILE" 2>/dev/null | grep -v '^-' | grep -v '^$' | head -1 || echo "")
        if [ -z "$RESEARCH_QUERY" ]; then
            RESEARCH_QUERY="Lean4 formal verification of geometric theorems, Tamari lattice, and binary tree enumeration"
        fi
        # manage.sh handles: parallel vmtouch + ranking, then starts 35B
        cd "$LLOCO_DIR"
        ./manage.sh swap 35b --query "$RESEARCH_QUERY" 2>&1 | tee -a "$LOGFILE"
        # Wait for 35B (manage.sh starts it + server init takes ~6 min)
        log "Waiting for 35B to be ready (checking every 15s, up to 10min)..."
        for i in $(seq 1 40); do
            sleep 15
            if curl -sf "http://localhost:$PORT_35B/v1/models" > /dev/null 2>&1; then
                log "35B ready after $((i * 15))s"
                break
            fi
            if [ $i -eq 40 ]; then
                log "ERROR: 35B did not become ready within 10 minutes"
            fi
        done
    else
        log "[dry-run] Would run: manage.sh swap 35b --query \"...\""
    fi
fi

# ---- 2. Run full pipeline (capped at 144 files via --top-k for ~3h budget) ----
RANKING_FILE="/tmp/lasercortex_ranking.json"
log "Running full pipeline (generate_phonebook --mode full)..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would run: python3 generate_phonebook.py --mode full --ranking-file \"$RANKING_FILE\" \"$REPO_DIR\""
else
    cd "$ON_DIR"
    RANKING_ARGS=""
    if [ -f "$RANKING_FILE" ]; then
        RANKING_ARGS="--ranking-file $RANKING_FILE"
        log "  Using ranking file: $RANKING_FILE"
    else
        log "  No ranking file found — processing all changed files (may exceed 3h)"
    fi
    INCLUDE_ARGS=""
    MANUAL_QUEUE="${REPO_DIR}/.manual_pipeline_queue"
    if [ -f "$MANUAL_QUEUE" ] && [ -s "$MANUAL_QUEUE" ]; then
        INCLUDE_ARGS="--include-list $MANUAL_QUEUE"
        log "  Manual pipeline queue: $MANUAL_QUEUE ($(wc -l < "$MANUAL_QUEUE") entries)"
    fi
    python3 scripts/pipeline/generate_phonebook.py --mode full --top-k 144 $RANKING_ARGS $INCLUDE_ARGS "$REPO_DIR" 2>&1 | tee -a "$LOGFILE"
    log "Pipeline complete"

    # ── Clear manual pipeline queue ──
    if [ -f "$MANUAL_QUEUE" ]; then
        # Re-create with only the header template
        cat > "$MANUAL_QUEUE" <<'EOF'
# Manual Pipeline Queue — one file path per line
#
# Files listed here will be included in the next nightly pipeline run,
# even if they are inside excluded directories (.lake, _archive, etc).
# Paths can be absolute or relative to the repo root. Globs supported.
#
# Usage:
#   Add a line below during daytime work, then the next nightly batch
#   will automatically pick it up. The queue is cleared after processing.
#
# Examples:
#   .lake/packages/mathlib4/Mathlib/Data/List/Basic.lean
#   .lake/packages/mathlib4/Mathlib/Algebra/Group.lean
EOF
        log "  Cleared manual pipeline queue"
    fi
fi

# ---- 3. Sync docstrings (inject Deep Analysis → Lean) ----
log "Syncing docstrings (--inject)..."
if [ "$DRY_RUN" = true ]; then
    log "[dry-run] Would run: sync_docstrings.py --inject"
else
    python3 scripts/pipeline/sync_docstrings.py --inject 2>&1 | tee -a "$LOGFILE"
    log "Docstring sync complete"
fi

# ---- 4. Web research for logged questions ----
if [ "$PIPELINE_ONLY" = false ]; then
    if [ -f "$RESEARCH_FILE" ] && [ -s "$RESEARCH_FILE" ]; then
        log "Processing research questions from ${RESEARCH_FILE}..."
        if [ "$DRY_RUN" = true ]; then
            log "[dry-run] Would read questions and run web search for each"
        else
            cd "$REPO_DIR"
            while IFS= read -r line; do
                line="${line#\#}"
                line="$(echo "$line" | xargs)"
                [ -z "$line" ] && continue
                [[ "$line" == -* ]] && continue
                log "  Researching: $line"
                # For now, just log — replace with actual web search tool invocation
                python3 -c "
import sys; print(f'QUESTION: {sys.argv[1]}')
" "$line" 2>&1 | tee -a "$LOGFILE"
            done < "$RESEARCH_FILE"
            echo "" > "$RESEARCH_FILE"
            git add "$RESEARCH_FILE" 2>/dev/null || true
            git commit -m "nightly: clear processed research questions" || true
            log "Research complete"
        fi
    else
        log "No research questions found (${RESEARCH_FILE})"
    fi
else
    log "Skipping research (--pipeline-only)"
fi

# ---- 5. Commit any file changes ----
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

# ---- Done ----
log "=== Nightly Batch Complete ==="
log "35B left loaded on :$PORT_35B"
log "Swap back to 9B: $LLOCO_DIR/manage.sh swap 9b"
echo "" | tee -a "$LOGFILE"
echo "Log: $LOGFILE"
