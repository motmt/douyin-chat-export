#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.server.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "鏈嶅姟宸插湪杩愯 (PID $(cat "$PID_FILE"))锛岃鍏堣繍琛?stop.sh"
  exit 1
fi

# 鏋勫缓鍓嶇锛堝鏋?node_modules 瀛樺湪锛?
if [ -d "$DIR/frontend/node_modules" ]; then
  echo "鏋勫缓鍓嶇..."
  cd "$DIR/frontend" && npm run build 2>&1 | tail -3
  cd "$DIR"
else
  echo "鎻愮ず: 鍓嶇鏈畨瑁呬緷璧栵紝杩愯 cd frontend && npm install && npm run build"
  if [ ! -d "$DIR/frontend/dist" ]; then
    echo "閿欒: frontend/dist 涓嶅瓨鍦紝鏃犳硶鍚姩"
    exit 1
  fi
fi

# 鍚姩鍚庣锛堝悓鏃?serve 鍓嶇 dist锛?
nohup "$DIR/venv/bin/python3" -m uvicorn backend.main:app \
  --host 127.0.0.1 --port 8001 \
  > "$DIR/.server.log" 2>&1 &

echo $! > "$PID_FILE"
sleep 1

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "鉁?鏈嶅姟宸插惎鍔?(PID $(cat "$PID_FILE"))"
  echo "  娴忚鍣ㄨ闂? http://127.0.0.1:8001"
  open "http://127.0.0.1:8001"
else
  echo "鉁?鍚姩澶辫触锛屾煡鐪嬫棩蹇? $DIR/.server.log"
  rm -f "$PID_FILE"
  exit 1
fi

