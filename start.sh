#!/bin/bash
set -e

echo "Starting FastAPI Server on 0.0.0.0:8000..."
python -m uvicorn server:app --host 0.0.0.0 --port 8000 &

echo "Starting LiveKit Voice Agent Worker..."
python agent.py start &

# Keep container alive by waiting for background processes
wait -n 2>/dev/null || wait
