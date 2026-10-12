#!/usr/bin/env bash
# Push the codex-parallel sandbox image to Tencent TCR (enterprise).
#
# Usage:
#   bash scripts/push_sandbox_image.sh
#   IMAGE_TAG=v2 bash scripts/push_sandbox_image.sh
#
# Requires: docker login tcr-rl.tencentcloudcr.com

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

FULL_TAG="${CCR_REGISTRY}/${CCR_NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[push] ERROR: docker not found."
  exit 1
fi

echo "[push] pushing ${FULL_TAG}"
docker push "${FULL_TAG}"
echo "[push] OK"
echo "[push] next: update configs/sandbox_tool.json CustomConfiguration.Image to:"
echo "        ${FULL_TAG}"
echo "[push] then create/register the Tool (see docker/sandbox/BUILD.md §3):"
echo "        tccli ags CreateSandboxTool --region ap-beijing --cli-input-json file://configs/sandbox_tool.json"
