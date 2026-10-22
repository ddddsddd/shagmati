#!/usr/bin/env bash
# Запускает оба сервера; Ctrl+C останавливает их.
cd "$(dirname "$0")"
python3 ai_server.py &
AI_PID=$!
python3 server.py &
GAME_PID=$!
trap 'kill $AI_PID $GAME_PID 2>/dev/null' EXIT INT TERM
sleep 1
echo "Открой в браузере: http://127.0.0.1:8000"
wait
