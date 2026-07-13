#!/bin/bash
# run_batch.sh — start mineru-api and launch batch_parse, fully detached
# Current host: 1× Tesla V100 — use deploy_api.sh (not dual-GPU router).
set -e

cd /home/duguex/mineru_wrapper

# Clean any leftover
pkill -9 -f "mineru-router" 2>/dev/null || true
pkill -9 -f "mineru-api" 2>/dev/null || true
pkill -9 -f "batch_parse.py" 2>/dev/null || true
sleep 2

# Start single-process API (one V100). Prefer --worker-conc 1 while ollama
# or other processes hold most of the 32GB VRAM.
setsid bash deploy_api.sh --host 127.0.0.1 --worker-conc 1 \
    > /dev/null 2>&1 < /dev/null &
disown

# Wait for API health
echo "Waiting for mineru-api..."
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:8001/health > /dev/null 2>&1; then
        echo "API ready"
        curl -s http://127.0.0.1:8001/health || true
        echo
        break
    fi
    sleep 2
done

# Launch batch_parse (concurrency 1 on single V100 with limited free VRAM)
echo "Starting batch_parse..."
setsid python3 /home/duguex/mineru_wrapper/batch_parse.py \
    --src /mnt/shared/home/c606/wjx/no_md_pdfs \
    --output /mnt/shared/mineru_batch_output \
    --url http://127.0.0.1:8001 \
    --concurrency 1 \
    --max-retries 3 \
    --retry-delay 30 \
    > /mnt/shared/mineru_logs/batch_parse.log 2>&1 < /dev/null &
disown

BATCH_PID=$!
echo "batch_parse PID: $BATCH_PID"
echo "Log: /mnt/shared/mineru_logs/batch_parse.log"
echo "Status: python3 /home/duguex/mineru_wrapper/batch_status.py /mnt/shared/mineru_batch_output/parsed/"
echo ""
echo "Both processes detached. Exiting..."
