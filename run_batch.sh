#!/bin/bash
# run_batch.sh — start router and launch batch_parse, fully detached
set -e

cd /home/duguex/mineru_wrapper

# Clean any leftover
pkill -9 -f "mineru-router" 2>/dev/null || true
pkill -9 -f "mineru-api" 2>/dev/null || true
pkill -9 -f "batch_parse.py" 2>/dev/null || true
sleep 2

# Start router
setsid bash deploy_router.sh --host 127.0.0.1 --worker-conc 2 \
    > /dev/null 2>&1 < /dev/null &
disown

# Wait for router health
echo "Waiting for router..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8002/health > /dev/null 2>&1; then
        echo "Router ready"
        curl -s http://127.0.0.1:8002/health | python3 -c "
import json, sys; h=json.load(sys.stdin)
for s in h['servers']: print(f'  {s[\"server_id\"]} gpu={s[\"gpu\"]} healthy={s[\"healthy\"]}')
"
        break
    fi
    sleep 2
done

# Launch batch_parse
echo "Starting batch_parse..."
setsid python3 /home/duguex/mineru_wrapper/batch_parse.py \
    --src /mnt/shared/home/c606/wjx/no_md_pdfs \
    --output /mnt/shared/mineru_batch_output \
    --url http://127.0.0.1:8002 \
    --concurrency 4 \
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