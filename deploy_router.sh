#!/bin/bash
# deploy_router.sh — Start minerU Router for CUDA multi-GPU (or single-GPU) deployment
#
# Spawns one mineru-api worker per GPU, isolates each via
# CUDA_VISIBLE_DEVICES, and load-balances across them.
#
# Usage:
#   ./deploy_router.sh                     # auto-detect GPUs, :8002, 0.0.0.0
#   ./deploy_router.sh --port 8000         # custom port
#   ./deploy_router.sh --host 127.0.0.1    # localhost only
#   ./deploy_router.sh --worker-conc 1     # requests per worker (default 2)
#   MINERU_ROUTER_LOCAL_GPUS=0,1 ./deploy_router.sh  # explicit GPU list
#
# On NVIDIA, stock CUDA_VISIBLE_DEVICES isolation is sufficient.
# This host currently has 1× V100 — default is auto-detect, not hard-coded 0,1.
set -eo pipefail

# ---- Config ------------------------------------------------------------
HOST="${MINERU_ROUTER_HOST:-0.0.0.0}"
PORT="${MINERU_ROUTER_PORT:-8002}"
WORKER_CONCURRENCY="${MINERU_API_MAX_CONCURRENT_REQUESTS:-2}"
OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-/mnt/shared/mineru_api_output}"
# Auto-detect NVIDIA GPUs when unset (single V100 → "0"; dual → "0,1")
if [ -z "${MINERU_ROUTER_LOCAL_GPUS:-}" ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        _ngpu=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')
        if [ "${_ngpu:-0}" -gt 0 ]; then
            LOCAL_GPUS=$(seq -s, 0 $((_ngpu - 1)))
        else
            LOCAL_GPUS=0
        fi
    else
        LOCAL_GPUS=0
    fi
else
    LOCAL_GPUS="$MINERU_ROUTER_LOCAL_GPUS"
fi
# ------------------------------------------------------------------------

# Parse CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --worker-conc) WORKER_CONCURRENCY="$2"; shift 2 ;;
        --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
        --local-gpus) LOCAL_GPUS="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

# Source CUDA environment
ENV_SCRIPT="$HOME/mineru-cuda/mineru-cuda-env.sh"
if [ -f "$ENV_SCRIPT" ]; then
    # shellcheck disable=SC1090
    source "$ENV_SCRIPT"
else
    echo "Warning: $ENV_SCRIPT not found" >&2
fi

# Parent sees the same GPU set the router will hand out to workers
export CUDA_VISIBLE_DEVICES="$LOCAL_GPUS"
unset HIP_VISIBLE_DEVICES ROCM_HOME 2>/dev/null || true
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
echo "  GPUs:        $LOCAL_GPUS (worker isolation via CUDA_VISIBLE_DEVICES)" | tee -a "$LOG_FILE"
echo "  Per-worker concurrency: $WORKER_CONCURRENCY" | tee -a "$LOG_FILE"
echo "  Output root: $OUTPUT_ROOT" | tee -a "$LOG_FILE"
echo "  Backend:     pipeline (recommended on V100)" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"
echo "Router docs: http://$HOST:$PORT/docs" | tee -a "$LOG_FILE"
echo "Health:      http://$HOST:$PORT/health" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

conda run -n mineru --no-capture-output \
    mineru-router --host "$HOST" --port "$PORT" --local-gpus="$LOCAL_GPUS" >> "$LOG_FILE" 2>&1
