#!/usr/bin/env bash
# Static checks on docker/sandbox/Dockerfile (no docker daemon required).
# Enforces Agent Runtime snapshot constraints:
#   no USER / no WORKDIR / no ENV / no ENTRYPOINT override.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DF="${ROOT}/docker/sandbox/Dockerfile"

if [[ ! -f "$DF" ]]; then
  echo "[validate] missing $DF"
  exit 1
fi

fail=0
check_forbidden() {
  local pattern="$1"
  local msg="$2"
  if grep -qiE "$pattern" "$DF"; then
    echo "[validate] FAIL: $msg"
    grep -niE "$pattern" "$DF" || true
    fail=1
  fi
}

check_forbidden '^[[:space:]]*USER[[:space:]]' "USER must not be set (snapshot requires root)"
check_forbidden '^[[:space:]]*WORKDIR[[:space:]]' "WORKDIR must not be set (snapshot requires /)"
check_forbidden '^[[:space:]]*ENV[[:space:]]' "ENV must not be set in Dockerfile (use API Env)"
check_forbidden '^[[:space:]]*ENTRYPOINT[[:space:]]' "ENTRYPOINT must not be overridden (keep /init from base)"

if [[ "$fail" -ne 0 ]]; then
  echo "[validate] Dockerfile violates Agent Runtime snapshot rules."
  exit 1
fi

if ! grep -q 'requirements.txt' "$DF"; then
  echo "[validate] WARN: Dockerfile does not reference requirements.txt"
fi

echo "[validate] Dockerfile OK (snapshot-compatible: no USER/WORKDIR/ENV/ENTRYPOINT)"
