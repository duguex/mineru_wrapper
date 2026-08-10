#!/bin/bash
# deploy.sh — start a minerU API server (single process) or router (per-GPU
# workers). One source of truth for env sourcing, GPU wiring, banner, and the
# timestamped log; the old deploy_api.sh / deploy_router.sh are this script
# with the mode already chosen.
#
# Usage:
#   ./deploy.sh api                       # mineru-api on :8001, GPU 0
#   ./deploy.sh router                    # mineru-router on :8002, auto-detected GPUs
#   ./deploy.sh api --port 8000           # custom port
#   ./deploy.sh router --local-gpus 0,1   # explicit GPU list (router only)
#   ./deploy.sh api --dry-run             # print what would run, don't execute
#
# Env overrides keep their historic names: MINERU_API_HOST / MINERU_ROUTER_HOST,
# MINERU_API_PORT / MINERU_ROUTER_PORT, MINERU_API_MAX_CONCURRENT_REQUESTS,
# MINERU_API_OUTPUT_ROOT, MINERU_LOG_DIR, MINERU_ROUTER_LOCAL_GPUS.

set -eo pipefail  # no -u: env script may reference optional vars

MODE="${1:?usage: deploy.sh api|router [--host H] [--port P] [--worker-conc N] [--output-root D] [--local-gpus G] [--dry-run]}"
shift

case "$MODE" in
    api)    BIN="mineru-api"    ;;
    router) BIN="mineru-router" ;;
    *) echo "Unknown mode: $MODE (use api or router)" >&2; exit 1 ;;
esac

# ---- Config: env defaults, then CLI overrides --------------------------
if [ "$MODE" = api ]; then
    HOST="${MINERU_API_HOST:-0.0.0.0}"
    PORT="${MINERU_API_PORT:-8001}"
else
    HOST="${MINERU_ROUTER_HOST:-0.0.0.0}"
    PORT="${MINERU_ROUTER_PORT:-8002}"
fi
WORKER_CONCURRENCY="${MINERU_API_MAX_CONCURRENT_REQUESTS:-2}"
OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-/mnt/shared/mineru_api_output}"
LOCAL_GPUS="${MINERU_ROUTER_LOCAL_GPUS:-}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --worker-conc) WORKER_CONCURRENCY="$2"; shift 2 ;;
        --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
        --local-gpus)
            if [ "$MODE" != router ]; then
                echo "Warning: --local-gpus only applies to router mode, ignoring" >&2
            else
                LOCAL_GPUS="$2"
            fi
            shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Unknown: $1" >&2; exit 1 ;;
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

# ---- Mode-specific GPU wiring ------------------------------------------
if [ "$MODE" = api ]; then
    # Single process sees GPU 0; minerU's internal scheduling uses it.
    export CUDA_VISIBLE_DEVICES=0
    export MINERU_API_ENABLE_FASTAPI_DOCS=1
else
    # Parent sees the same GPU set the router will hand out to workers.
    if [ -z "$LOCAL_GPUS" ]; then
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
    fi
    export CUDA_VISIBLE_DEVICES="$LOCAL_GPUS"
    unset HIP_VISIBLE_DEVICES ROCM_HOME 2>/dev/null || true
    export MINERU_API_ENABLE_FASTAPI_DOCS=0
fi

export MINERU_API_MAX_CONCURRENT_REQUESTS="$WORKER_CONCURRENCY"
export MINERU_API_OUTPUT_ROOT="$OUTPUT_ROOT"
mkdir -p "$OUTPUT_ROOT"

# Log directory (must be outside $HOME for systemd ProtectHome=read-only)
LOG_DIR="${MINERU_LOG_DIR:-/mnt/shared/mineru_logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/${MODE}_$(date +%Y%m%d_%H%M%S).log"

# ---- Banner (per-mode labels, one block) -------------------------------
if [ "$MODE" = api ]; then
    echo "=== minerU API Server ===" | tee -a "$LOG_FILE"
else
    echo "=== minerU Router ===" | tee -a "$LOG_FILE"
fi
echo "  Host:        $HOST" | tee -a "$LOG_FILE"
echo "  Port:        $PORT" | tee -a "$LOG_FILE"
echo "  Output root: $OUTPUT_ROOT" | tee -a "$LOG_FILE"
if [ "$MODE" = api ]; then
    echo "  GPU device:  $CUDA_VISIBLE_DEVICES" | tee -a "$LOG_FILE"
    echo "  Concurrency: $MINERU_API_MAX_CONCURRENT_REQUESTS" | tee -a "$LOG_FILE"
else
    echo "  GPUs:        $LOCAL_GPUS (worker isolation via CUDA_VISIBLE_DEVICES)" | tee -a "$LOG_FILE"
    echo "  Per-worker concurrency: $WORKER_CONCURRENCY" | tee -a "$LOG_FILE"
fi
echo "  Backend:     pipeline (recommended on V100)" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"
if [ "$MODE" = api ]; then
    echo "API docs:  http://$HOST:$PORT/docs" | tee -a "$LOG_FILE"
else
    echo "Router docs: http://$HOST:$PORT/docs" | tee -a "$LOG_FILE"
fi
echo "Health:    http://$HOST:$PORT/health" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# ---- Launch (always exec; the unit or the foreground terminal owns the
#      process tree — this replaces the old api=exec / router=noexec split).
LAUNCH=(conda run -n mineru --no-capture-output "$BIN" --host "$HOST" --port "$PORT")
if [ "$MODE" = router ]; then
    LAUNCH+=(--local-gpus="$LOCAL_GPUS")
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN — would execute:" >&2
    echo "  ${LAUNCH[*]} >> $LOG_FILE 2>&1" >&2
    exit 0
fi

exec "${LAUNCH[@]}" >> "$LOG_FILE" 2>&1