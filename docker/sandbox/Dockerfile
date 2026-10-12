# Codex Parallel sandbox image (Tencent Agent Runtime code-interpreter extension)
#
# Purpose: one sandbox = one `codex exec` worker. The hub (codex_parallel_infra)
# creates an instance per task item, uploads auth.json into CODEX_HOME, runs
# `codex exec --json -o result-last.md -- "<prompt>"`, downloads the result.
#
# What's baked in:
#   - Node.js + @openai/codex CLI (the ONLY agent binary that matters here)
#   - Common shell/file tooling + python3 for prompt glue scripts
# What's NOT baked in (per-instance, injected by the hub at runtime):
#   - CODEX_HOME / auth.json  → per-token, written by codex_runner.prepare_auth
#   - model config            → codex_config on the task, passed via -m flags
#
# Official guide: https://cloud.tencent.com/document/product/1814/129691
#
# Rules (Agent Runtime snapshot / pause-resume compatible — enforced by
# scripts/validate_sandbox_dockerfile.sh):
#   - Do NOT set USER (must stay root)
#   - Do NOT set WORKDIR (must stay /)
#   - Do NOT set ENV in Dockerfile (pass via API Env at instance creation)
#   - Do NOT override ENTRYPOINT (keep /init from base image)
#   - Only add dependencies via RUN/COPY; CMD/ENTRYPOINT inherit from base
#
# Build (on a Mac with docker):
#   docker build --platform linux/amd64 -t <registry>/<ns>/codex-parallel-sandbox:v1 \
#       -f docker/sandbox/Dockerfile docker/sandbox/
# See docker/sandbox/BUILD.md for the full build+push+tool-creation runbook.
#
# Platform: linux/amd64 ONLY (Agent Runtime requirement).

ARG SANDBOX_BASE_IMAGE=tcr-rl.tencentcloudcr.com/agentos-cl-namespace/sandbox-code:latest
FROM ${SANDBOX_BASE_IMAGE}

# Codex CLI version. Pinned for reproducible母版; override with --build-arg.
# Check latest: npm view @openai/codex version
ARG CODEX_VERSION=0.162.1

# Node major for the nodesource apt repo. Codex ships prebuilt native
# binaries; Node LTS is enough (codex is invoked via its npm bin shim).
ARG NODE_MAJOR=22

# 1. System tooling the codex agent commonly shells out to.
#    ripgrep/fd: codex's built-in search. git/jq/zip: workspace glue.
#    No model/inference deps — codex talks to the OpenAI API over the network.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        git jq ripgrep fd-find unzip zip less procps \
    && rm -rf /var/lib/apt/lists/*

# 2. Node.js (codex CLI is an npm package with a Node launcher shim).
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# 3. Codex CLI — the in-sandbox agent. Global install puts `codex` on PATH.
RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && codex --version

# 4. Python glue (the hub's mock/tests + light prompt-side processing).
#    --break-system-packages: AGS base uses system Python (no venv).
COPY requirements.txt /tmp/codex-parallel-sandbox-requirements.txt
RUN pip install --no-cache-dir --break-system-packages \
        -r /tmp/codex-parallel-sandbox-requirements.txt \
    && rm -f /tmp/codex-parallel-sandbox-requirements.txt

# 5. Smoke-check the binary landed and runs (fail the build early if not).
RUN codex --version && codex exec --help >/dev/null 2>&1 || \
        (echo "codex exec not functional" >&2; exit 1)
