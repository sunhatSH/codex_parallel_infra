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

## 网络注意（重要——实测结论 2026-10-12）

**沙箱必须能出网访问 Codex API 端点。实测发现北京区域的沙箱出不去 OpenAI：**

| 测试 | 结果 |
|---|---|
| VPC 模式（subnet-ljfuh7ln） | `api.openai.com` 超时（000） |
| PUBLIC 模式（`NetworkMode: "PUBLIC"`，Tool `sdt-4j86lmyw`） | `api.openai.com` DNS 被污染（解析出 Facebook IPv6）+ 直连全部 IP 超时；`auth.openai.com` 返回 403（Cloudflare 区域拦截）；`status.openai.com` 反而可达 |

结论：这是 GFW 对 OpenAI 域名的 DNS 污染 + IP 封锁 + Cloudflare 区域拦截，
腾讯云北京出口绕不过去。**沙箱内 codex 会一直卡到超时**（不是账号问题，
网络层就不通）。

可选出路（按推荐排序）：

1. **境外区域开沙箱**：Agent Runtime 如有新加坡/硅谷区域，Tool 用境外区域
   创建，网络直连 OpenAI。改 `--region` + 重建 Tool 即可，镜像复用。
2. **沙箱内走代理**：在 VPC 内自建一个能出境的 HTTP 代理（或买商业代理），
   镜像里给 codex 注入 `HTTPS_PROXY`。hub 的 `codex_config.extra_args`
   可以传 `-c network_proxy.url=...`，或者后端 create 时注入 env。
3. **中转 API 端点**：如果 Codex 凭据支持自定义 base_url（自建网关反代
   api.openai.com），改 codex 的 model_providers 配置指向网关。

在这之前，`local` backend（本机/境外机器直跑）是唯一能真跑 codex 的路径。
