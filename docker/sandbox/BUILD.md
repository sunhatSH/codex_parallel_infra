# 沙箱镜像构建手册（Mac 上执行）

镜像内容：`sandbox-code` 基底 + Node.js + `@openai/codex` CLI。
一个沙箱实例 = 一个 codex exec worker。Hub 通过 E2B SDK 起实例、传
CODEX_HOME/auth.json、跑 `codex exec`、取回结果。

**注意平台**：Agent Runtime 只认 `linux/amd64`，Mac 上构建必须带
`--platform linux/amd64`（Apple Silicon 走 Rosetta 模拟，慢但可用）。

---

## 0. 前置

```bash
# Docker Desktop 已装并启动
docker version

# 登录腾讯云 TCR 企业版（密码在容器镜像服务控制台获取，用户名=账号ID）
docker login tcr-rl.tencentcloudcr.com

# 拉基底镜像（约 1-2 GB）
docker pull tcr-rl.tencentcloudcr.com/agentos-cl-namespace/sandbox-code:latest
```

## 1. 构建

```bash
cd codex_parallel_infra
bash scripts/build_sandbox_image.sh
```

脚本自动：
- 校验 Dockerfile 符合快照约束（无 USER/WORKDIR/ENV/ENTRYPOINT）
- `docker build --platform linux/amd64` 打出
  `tcr-rl.tencentcloudcr.com/agentos-cl-namespace/codex-parallel-sandbox:v1`

常用变体：

```bash
# 换 codex 版本（查最新: npm view @openai/codex version）
CODEX_VERSION=0.162.1 bash scripts/build_sandbox_image.sh

# 改了 Dockerfile/requirements 后强制全量重建
BUILD_NO_CACHE=1 bash scripts/build_sandbox_image.sh

# 只想本地构建不打 tag 的远程前缀 —— 编辑 docker/sandbox/image.env
```

## 2. 本地验证（推之前必做）

```bash
# codex 二进制能跑（amd64 模拟下 ~1min）
docker run --rm --platform linux/amd64 \
    tcr-rl.tencentcloudcr.com/agentos-cl-namespace/codex-parallel-sandbox:v1 \
    codex --version

# exec 子命令可用（不需要真登录，验证 CLI 完整性）
docker run --rm --platform linux/amd64 \
    tcr-rl.tencentcloudcr.com/agentos-cl-namespace/codex-parallel-sandbox:v1 \
    sh -c 'codex exec --help >/dev/null && echo exec-ok'
```

想全链路冒烟（可选，需要一个真 token）：

```bash
docker run --rm --platform linux/amd64 -it \
    -e CODEX_HOME=/tmp/ch \
    tcr-rl.tencentcloudcr.com/agentos-cl-namespace/codex-parallel-sandbox:v1 \
    sh -c 'mkdir -p /tmp/ch && echo "{\"OPENAI_API_KEY\": \"sk-...\"}" > /tmp/ch/auth.json && \
           cd /tmp && codex exec --skip-git-repo-check --json "reply with OK"'
```

## 3. 推送

```bash
bash scripts/push_sandbox_image.sh
```

## 4. 注册 Agent Runtime Tool（一次性，在能跑 tccli 的机器上）

```bash
# 腾讯云 API 密钥（CAM 控制台 https://console.cloud.tencent.com/cam/capi）
export TENCENTCLOUD_SECRET_ID=...
export TENCENTCLOUD_SECRET_KEY=...
pip install -q tccli

# 确认 configs/sandbox_tool.json 的 Image 字段 = 推上去的 FULL_TAG，然后：
tccli ags CreateSandboxTool --region ap-beijing --cli-input-json file://configs/sandbox_tool.json

# 拿到 ToolId。再建一个 API key 给 E2B SDK 用：
tccli ags CreateAPIKey --region ap-beijing --Name codex-parallel
# 输出 E2B_API_KEY（只显示一次，存好）
```

## 5. Hub 侧启用 e2b backend

```bash
# 部署机上
pip install -e ".[e2b]"
export E2B_API_KEY=<上一步的 key>
export E2B_DOMAIN=ap-beijing.tencentags.com   # 与管理员确认
export E2B_TEMPLATE=codex-parallel-sandbox    # ToolName

# 提交任务时切 backend
curl -X POST localhost:8787/api/tasks -H 'Content-Type: application/json' \
  -d '{"name": "e2b-run", "prompt_template": "过滤: {{item}}",
       "items": ["d1"], "concurrency": 64, "backend": "e2b"}'
```

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `docker/sandbox/Dockerfile` | 镜像定义（快照约束已内置） |
| `docker/sandbox/requirements.txt` | 镜像内 Python 依赖（保持最小） |
| `docker/sandbox/image.env` | registry/命名空间/tag（gitignore） |
| `scripts/build_sandbox_image.sh` | 构建入口（Mac 上跑） |
| `scripts/push_sandbox_image.sh` | 推送入口 |
| `scripts/validate_sandbox_dockerfile.sh` | 快照约束静态校验 |
| `configs/sandbox_tool.json` | Agent Runtime Tool 注册配置 |

## 网络注意

沙箱内 codex 要出网访问 Codex API 端点。`configs/sandbox_tool.json`
当前配的是 VPC 模式（subnet-ljfuh7ln / sg-irz5h5ed，沿用 agentic_cl_research
的）。如果 VPC 内不通公网，把 `NetworkMode` 改成 `"PUBLIC"` 再重新
`CreateSandboxTool`。
