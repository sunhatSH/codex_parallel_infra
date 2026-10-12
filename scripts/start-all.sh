#!/usr/bin/env bash
# Start BOTH the hub backend and the React frontend, in tmux.
#
# Usage:
#   bash scripts/start-all.sh [start|stop|status|restart]
#
# Sessions:
#   codex-hub  — FastAPI backend on :8787  (data under ./data)
#   codex-web  — Vite dev server  on :5173 (proxies /api -> :8787)
#
# Production alternative: build the frontend once (npm run build in web-react/)
# and serve web-react/dist from nginx/any static host; the backend stays as-is.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-/mnt/afs_toolcall/sunhao4/miniconda3/bin/python3}"
NPM="${NPM:-$(command -v npm || echo /mnt/afs_toolcall/sunhao4/node/bin/npm)}"
HUB_PORT="${HUB_PORT:-8787}"
WEB_PORT="${WEB_PORT:-5173}"
HUB_SESSION="codex-hub"
WEB_SESSION="codex-web"

cd "$ROOT"

start_hub() {
  if tmux has-session -t "$HUB_SESSION" 2>/dev/null; then
    echo "[hub] already running (tmux: $HUB_SESSION)"
    return
  fi
  "$PY" -c "import codex_parallel, codex_parallel_cli" 2>/dev/null \
    || "$PY" -m pip install -q -e "$ROOT"
  mkdir -p data
  tmux new -d -s "$HUB_SESSION" \
    "$PY -m codex_parallel_cli serve --port $HUB_PORT 2>&1 | tee -a data/hub.log"
  echo "[hub] started: tmux=$HUB_SESSION port=$HUB_PORT"
}

start_web() {
  if tmux has-session -t "$WEB_SESSION" 2>/dev/null; then
    echo "[web] already running (tmux: $WEB_SESSION)"
    return
  fi
  if [ ! -d web-react/node_modules ]; then
    echo "[web] installing frontend deps (first run)..."
    (cd web-react && "$NPM" install)
  fi
  tmux new -d -s "$WEB_SESSION" \
    "cd '$ROOT/web-react' && $NPM run dev -- --host 0.0.0.0 --port $WEB_PORT 2>&1 | tee -a '$ROOT/data/web.log'"
  echo "[web] started: tmux=$WEB_SESSION port=$WEB_PORT"
}

wait_up() {  # wait_up <url> <name>
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "$1"; then
      echo "[ok] $2 is up: $1"
      return 0
    fi
    sleep 0.5
  done
  echo "[FAIL] $2 did not come up: $1" >&2
  return 1
}

case "${1:-start}" in
  start)
    start_hub
    start_web
    echo ""
    wait_up "http://127.0.0.1:$HUB_PORT/api/health" "backend"
    wait_up "http://127.0.0.1:$WEB_PORT/" "frontend"
    echo ""
    echo "面板:  http://127.0.0.1:$WEB_PORT/"
    echo "API:   http://127.0.0.1:$HUB_PORT/docs"
    echo "查看:  tmux attach -t $HUB_SESSION | tmux attach -t $WEB_SESSION"
    ;;
  stop)
    tmux kill-session -t "$WEB_SESSION" 2>/dev/null && echo "[web] stopped" || echo "[web] no session"
    tmux kill-session -t "$HUB_SESSION" 2>/dev/null && echo "[hub] stopped" || echo "[hub] no session"
    ;;
  status)
    for s in "$HUB_SESSION" "$WEB_SESSION"; do
      if tmux has-session -t "$s" 2>/dev/null; then echo "$s: running"; else echo "$s: stopped"; fi
    done
    curl -sf "http://127.0.0.1:$HUB_PORT/api/health" && echo || echo "backend api: unreachable"
    curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/" && echo "frontend: up" || echo "frontend: unreachable"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  *)
    echo "usage: $0 [start|stop|status|restart]" >&2
    exit 1
    ;;
esac
