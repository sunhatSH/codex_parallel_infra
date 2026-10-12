#!/usr/bin/env bash
# In-sandbox watchdog: a plain shell supervisor (NOT an agent) that watches
# the codex process and persists a machine-readable status file the hub can
# read even if the streaming connection to the sandbox dies (AGS gateway
# GOAWAY / network partition).
#
# Contract with the hub (codex_runner.run):
#   /tmp/.watchdog/status     — final state, one JSON line, written on exit:
#     {"phase":"started","pid":N,"ts":...}                  — right after spawn
#     {"phase":"exited","pid":N,"code":N,"signal":N,"ts":...} — codex ended
#     {"phase":"lost","pid":N,"reason":"...","ts":...}      — proc vanished
#     {"phase":"heartbeat","ts":...}                        — periodic (alive)
#   /tmp/.watchdog/exit       — watchdog's own exit code
#
# Usage (started by the hub right after creating the sandbox):
#   watchdog.sh <codex-pid> &
#
# Notes:
#   - busybox/coreutils compatible; no python needed on the sandbox side
#   - writes are atomic (tmp+mv) so the hub never reads a half line
#   - watchdog exits shortly after codex; it does NOT keep the sandbox alive
set -u

PID="${1:?usage: watchdog.sh <codex-pid>}"
DIR="/tmp/.watchdog"
mkdir -p "$DIR"

emit() {  # emit <phase> <extra-kv>...
  local phase="$1"; shift
  local kv="\"phase\":\"$phase\",\"pid\":$PID"
  for pair in "$@"; do kv="$kv,$pair"; done
  printf '{%s}\n' "$kv" > "$DIR/.status.tmp"
  mv "$DIR/.status.tmp" "$DIR/status"
}

now() { date +%s; }

emit "started"

# Watch loop: poll /proc/<pid> — cheap, local, no network involved.
while [ -d "/proc/$PID" ]; do
  emit "heartbeat" "\"ts\":$(now)"
  sleep 5
done

# Process is gone — figure out how.
if [ -f "$DIR/exit_code" ]; then
  # codex_runner writes exit_code when it can; watchdog is the fallback path
  emit "exited" "\"code\":$(cat "$DIR/exit_code" 2>/dev/null || echo -1)"
else
  emit "lost" "\"reason\":\"proc-vanished\""
fi

echo 0 > "$DIR/exit"
exit 0
