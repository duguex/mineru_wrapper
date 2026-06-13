#!/bin/bash
# deploy_api.sh — Start minerU FastAPI server for ROCm (persistent deployment)
#
# Usage:
#   ./deploy_api.sh                    # start on :8001, bind 0.0.0.0
#   ./deploy_api.sh --port 8000        # custom port
#   ./deploy_api.sh --host 127.0.0.1   # localhost only
#
# Clients send PDFs via HTTP multipart; see api_client.py for examples.

set -eo pipefail  # no -u: mineru-rocm-env.sh uses unbound LD_LIBRARY_PATH

# ---- Config ------------------------------------------------------------
HOST="${MINERU_API_HOST:-0.0.0.0}"
PORT="${MINERU_API_PORT:-8001}"
OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-$HOME/mineru_api_output}"
# ------------------------------------------------------------------------

# Parse CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
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

# Override: GPU 0 is occupied by ollama/llama-server, use GPU 1
export HIP_VISIBLE_DEVICES=1

# ROCm stability: limit concurrent parsing to 1
export MINERU_API_MAX_CONCURRENT_REQUESTS=1

# Output root
export MINERU_API_OUTPUT_ROOT="$OUTPUT_ROOT"
mkdir -p "$OUTPUT_ROOT"

# Enable Swagger docs
export MINERU_API_ENABLE_FASTAPI_DOCS=1

# Log directory with timestamp (never overwritten)
LOG_DIR="${HOME}/logs/mineru"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/api_$(date +%Y%m%d_%H%M%S).log"

echo "=== minerU API Server ===" | tee -a "$LOG_FILE"
echo "  Host:        $HOST" | tee -a "$LOG_FILE"
echo "  Port:        $PORT" | tee -a "$LOG_FILE"
echo "  Output root: $OUTPUT_ROOT" | tee -a "$LOG_FILE"
echo "  GPU device:  $HIP_VISIBLE_DEVICES" | tee -a "$LOG_FILE"
echo "  Concurrency: $MINERU_API_MAX_CONCURRENT_REQUESTS" | tee -a "$LOG_FILE"
echo "  Backend:     pipeline (mandatory for ROCm)" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"
echo "API docs:  http://$HOST:$PORT/docs" | tee -a "$LOG_FILE"
echo "Health:    http://$HOST:$PORT/health" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Launch the API server (append all output to log)
# NOTE: clients MUST specify backend=pipeline (the default hybrid-auto-engine
# depends on CUDA vLLM which is unavailable on ROCm).
exec conda run -n torch_rocm72 --no-capture-output \
    mineru-api --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1
