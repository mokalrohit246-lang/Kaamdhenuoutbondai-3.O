#!/bin/bash
set -e
echo "Starting FastAPI Server on 0.0.0.0:8000..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

echo "Starting LiveKit Voice Agent Worker..."
python agent.py start &
AGENT_PID=$!

trap "kill -TERM $SERVER_PID $AGENT_PID 2>/dev/null || true" SIGINT SIGTERM
wait -n $SERVER_PID $AGENT_PID
