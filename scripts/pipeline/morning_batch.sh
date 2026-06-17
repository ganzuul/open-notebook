#!/usr/bin/env bash
# morning_batch.sh — Reverse swap: stop 35B, start 9B for daytime chat.
#
# Delegates to Docker compose in llocollama/. The 9B stays loaded until
# the next nightly swap or explicit stop.
#
# Usage:
#   ./morning_batch.sh              # stop 35B, start 9B
#   ./morning_batch.sh --dry-run    # show what would happen
#   ./morning_batch.sh --status     # check what's loaded

set -euo pipefail

LLOCO_DIR="/home/nos/labware/llocollama"
PORT_9B=11434

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---- Parse args ----
if [ "${1:-}" = "--status" ]; then
    exec "$LLOCO_DIR/manage.sh" status
fi
DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

log "=== Morning Swap: 35B → 9B ==="

if curl -sf "http://localhost:$PORT_9B/v1/models" > /dev/null 2>&1; then
    log "9B already running on :$PORT_9B"
    exit 0
fi

log "Swapping 35B → 9B via Docker..."
if [ "$DRY_RUN" = false ]; then
    cd "$LLOCO_DIR"
    docker compose stop model-35b 2>/dev/null || true
    docker compose up -d model-9b
    log "9B starting — will be ready in ~30s"
else
    log "[dry-run] Would: docker compose stop model-35b && docker compose up -d model-9b"
fi

log "=== Morning swap complete ==="
