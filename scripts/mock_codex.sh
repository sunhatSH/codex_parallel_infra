#!/usr/bin/env bash
# Programmable mock `codex` for tests. Behavior is driven by env vars:
#   MOCK_CODEX_MODE=ok            → exit 0, print one JSON agent_message event
#   MOCK_CODEX_MODE=auth          → exit 1, stderr "401 Unauthorized: invalid api key"
#   MOCK_CODEX_MODE=transient     → exit 1, stderr "429 rate limit exceeded"
#   MOCK_CODEX_MODE=hang          → sleep MOCK_CODEX_SLEEP (default 3600)
#   MOCK_CODEX_MODE=flaky         → fail until MOCK_CODEX_SUCCESS_AT attempts seen
#                                   (attempt count tracked in MOCK_STATE_FILE)
#   MOCK_CODEX_MODE=echo-secret   → print CODEX_HOME/auth.json contents (leak test)
# All modes also write "result-last.md" in cwd when succeeding.
set -u
MODE="${MOCK_CODEX_MODE:-ok}"
STATE_FILE="${MOCK_STATE_FILE:-/tmp/mock_codex_state}"

mkdir -p "$CODEX_HOME"

case "$MODE" in
  ok)
    echo '{"type":"agent_message","msg":"MOCK-RESULT-'$(date +%s)'"}'
    echo "mock codex final answer" > result-last.md
    exit 0
    ;;
  auth)
    echo "Error: 401 Unauthorized - invalid api key or account suspended" >&2
    exit 1
    ;;
  transient)
    echo "Error: 429 Too Many Requests - rate limit exceeded, retry later" >&2
    exit 1
    ;;
  hang)
    sleep "${MOCK_CODEX_SLEEP:-3600}"
    exit 1
    ;;
  flaky)
    n=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    n=$((n+1))
    echo "$n" > "$STATE_FILE"
    if [ "$n" -ge "${MOCK_CODEX_SUCCESS_AT:-3}" ]; then
      echo '{"type":"agent_message","msg":"MOCK-RESULT-after-failures"}'
      echo "recovered on attempt $n" > result-last.md
      exit 0
    fi
    echo "Error: 503 overloaded - temporarily unavailable" >&2
    exit 1
    ;;
  echo-secret)
    cat "$CODEX_HOME/auth.json"
    exit 0
    ;;
  *)
    echo "unknown MOCK_CODEX_MODE=$MODE" >&2
    exit 2
    ;;
esac
