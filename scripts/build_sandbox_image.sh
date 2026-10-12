#!/usr/bin/env bash
# Build the codex-parallel sandbox image (linux/amd64) — run on a Mac / any
# machine with docker. Then push with scripts/push_sandbox_image.sh.
#
# Prerequisites:
#   1. docker (Mac: Docker Desktop, Rosetta or amd64 emulation both fine)
#   2. docker login tcr-rl.tencentcloudcr.com  (CCR 密码, 控制台获取)
#   3. docker pull tcr-rl.tencentcloudcr.com/agentos-cl-namespace/sandbox-code:latest
#
# Usage:
#   bash scripts/build_sandbox_image.sh
#   CODEX_VERSION=0.162.1 bash scripts/build_sandbox_image.sh     # pin another codex
#   BUILD_NO_CACHE=1 bash scripts/build_sandbox_image.sh          # 无缓存重建

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/docker/sandbox/image.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

: "${CCR_REGISTRY:=tcr-rl.tencentcloudcr.com}"
: "${CCR_NAMESPACE:=REPLACE_WITH_YOUR_NAMESPACE}"
: "${IMAGE_NAME:=codex-parallel-sandbox}"
: "${IMAGE_TAG:=v1}"
: "${SANDBOX_BASE_IMAGE:=tcr-rl.tencentcloudcr.com/agentos-cl-namespace/sandbox-code:latest}"
: "${CODEX_VERSION:=0.162.1}"

FULL_TAG="${CCR_REGISTRY}/${CCR_NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "[build] validating Dockerfile constraints..."
bash "${ROOT}/scripts/validate_sandbox_dockerfile.sh"

if ! command -v docker >/dev/null 2>&1; then
  echo "[build] ERROR: docker not found. Install Docker Desktop (Mac) first."
  exit 1
fi

echo "[build] building ${FULL_TAG} (platform=linux/amd64)"
echo "[build] base image: ${SANDBOX_BASE_IMAGE}"
echo "[build] codex CLI : @openai/codex@${CODEX_VERSION}"

# 从头构建开关：
#   BUILD_NO_CACHE=1 → --no-cache + --pull（不用任何层缓存、强制拉新 base）。
# bash 3.2 (macOS 自带) 下空数组 + set -u 会报 unbound variable，
# 用 ${arr[@]+"${arr[@]}"} 展开习惯来兼容。
_BUILD_ARGS=()
if [[ "${BUILD_NO_CACHE:-0}" == "1" ]]; then
  _BUILD_ARGS+=(--no-cache --pull)
fi

docker build \
  --platform linux/amd64 \
  --build-arg SANDBOX_BASE_IMAGE="${SANDBOX_BASE_IMAGE}" \
  --build-arg CODEX_VERSION="${CODEX_VERSION}" \
  ${_BUILD_ARGS[@]+"${_BUILD_ARGS[@]}"} \
  -t "${FULL_TAG}" \
  -f "${ROOT}/docker/sandbox/Dockerfile" \
  "${ROOT}/docker/sandbox/"

echo ""
echo "[build] OK: ${FULL_TAG}"
echo "[build] smoke-test locally (amd64 emulation on Mac is slow, ~1min):"
echo "  docker run --rm --platform linux/amd64 ${FULL_TAG} codex --version"
echo "[build] then push:"
echo "  bash scripts/push_sandbox_image.sh"
