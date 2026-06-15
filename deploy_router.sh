#!/bin/bash
# deploy_router.sh — Start minerU Router for multi-GPU ROCm deployment
#
# Spawns one mineru-api worker per GPU, isolates each via
# HIP_VISIBLE_DEVICES, and load-balances across them.
#
# Usage:
#   ./deploy_router.sh                     # start on :8002, bind 0.0.0.0
#   ./deploy_router.sh --port 8000         # custom port
#   ./deploy_router.sh --host 127.0.0.1    # localhost only
#   ./deploy_router.sh --worker-conc 1     # requests per worker (default 2)
#
# Prerequisite: mineru/cli/router.py must have the ROCm HIP_VISIBLE_DEVICES
# patch applied (line 425-430). See CLAUDE.md for details.

set -eo pipefail

# ---- Config ------------------------------------------------------------
HOST="${MINERU_ROUTER_HOST:-0.0.0.0}"
PORT="${MINERU_ROUTER_PORT:-8002}"
WORKER_CONCURRENCY="${MINERU_API_MAX_CONCURRENT_REQUESTS:-2}"
OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-/mnt/shared/mineru_api_output}"
# ------------------------------------------------------------------------

# Parse CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --worker-conc) WORKER_CONCURRENCY="$2"; shift 2 ;;
        --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

# Source ROCm environment
ENV_SCRIPT="$HOME/mineru-rocm/mineru-rocm-env.sh"
if [ -f "$ENV_SCRIPT" ]; then
    # shellcheck disable=SC1090
    source "$ENV_SCRIPT"
else
    echo "Warning: $ENV_SCRIPT not found" >&2
fi

# Override: both GPUs visible to parent (router copies env to workers)
export HIP_VISIBLE_DEVICES=0,1

# Per-worker concurrency
export MINERU_API_MAX_CONCURRENT_REQUESTS="$WORKER_CONCURRENCY"

# Output root (router assigns subdirs per worker)
export MINERU_API_OUTPUT_ROOT="$OUTPUT_ROOT"
mkdir -p "$OUTPUT_ROOT"

# Disable Swagger on workers (router has its own docs)
export MINERU_API_ENABLE_FASTAPI_DOCS=0

# Log directory (must be outside $HOME for systemd ProtectHome=read-only)
LOG_DIR="${MINERU_LOG_DIR:-/mnt/shared/mineru_logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/router_$(date +%Y%m%d_%H%M%S).log"

echo "=== minerU Router ===" | tee -a "$LOG_FILE"
echo "  Host:        $HOST" | tee -a "$LOG_FILE"
echo "  Port:        $PORT" | tee -a "$LOG_FILE"
echo "  GPUs:        $HIP_VISIBLE_DEVICES (worker isolation via HIP_VISIBLE_DEVICES)" | tee -a "$LOG_FILE"
echo "  Per-worker concurrency: $WORKER_CONCURRENCY" | tee -a "$LOG_FILE"
echo "  Output root: $OUTPUT_ROOT" | tee -a "$LOG_FILE"
echo "  Backend:     pipeline (mandatory for ROCm)" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"
echo "Router docs: http://$HOST:$PORT/docs" | tee -a "$LOG_FILE"
echo "Health:      http://$HOST:$PORT/health" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

exec conda run -n torch_rocm72 --no-capture-output \
    mineru-router --host "$HOST" --port "$PORT" --local-gpus=auto >> "$LOG_FILE" 2>&1
