#!/usr/bin/env bash
# Deploy the hub as a background service (tmux, per project CLAUDE.md rule).
#
# Usage:
#   bash scripts/deploy.sh              # start hub in tmux session "codex-hub"
#   bash scripts/deploy.sh stop         # kill the tmux session
#   bash scripts/deploy.sh status       # tmux session state + API health
set -euo pipefail

PY="${PY:-/mnt/afs_toolcall/sunhao4/miniconda3/bin/python3}"
SESSION="${SESSION:-codex-hub}"
PORT="${PORT:-8787}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT"

case "${1:-start}" in
  start)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "[deploy] session '$SESSION' already running:"
      tmux list-sessions | grep "$SESSION"
      exit 0
    fi
    # install package (editable) if needed
    "$PY" -c "import codex_parallel" 2>/dev/null || "$PY" -m pip install -q -e "$ROOT"
    mkdir -p data
    tmux new -d -s "$SESSION" \
      "$PY -m codex_parallel.cli serve --port $PORT 2>&1 | tee -a data/hub.log"
    echo "[deploy] hub started in tmux session '$SESSION' on port $PORT"
    echo "[deploy] attach:  tmux attach -t $SESSION"
    echo "[deploy] panel:   http://127.0.0.1:$PORT/"
    ;;
  stop)
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "[deploy] stopped" || echo "[deploy] no session"
    ;;
  status)
    tmux has-session -t "$SESSION" 2>/dev/null && echo "tmux: running" || echo "tmux: stopped"
    curl -s "http://127.0.0.1:$PORT/api/health" && echo || echo "api: unreachable"
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 1
    ;;
esac
