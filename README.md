# codex-parallel-infra

统一命令中枢：把数据过滤任务 fan-out 到沙箱内并行运行的 Codex CLI。

## 架构

见 `docs/ARCHITECTURE.md`。三层分离：

```
cli/            CLI 包 (codex_parallel_cli) — HTTP 客户端, 可装在任意机器
web-react/      React 前端 (Vite) — 独立 dev server, proxy /api -> hub
src/codex_parallel/  后端 hub (FastAPI + SQLite + 调度器 + 沙箱 backend)
```

## 快速开始

```bash
# 1. 安装后端 + CLI
pip install -e .

# 2. 一键起前后端 (tmux: codex-hub + codex-web)
bash scripts/start-all.sh start
#    面板 http://127.0.0.1:5173/   API http://127.0.0.1:8787/docs

# 3. 批量导入 token (每行一个)
curl -X POST localhost:8787/api/tokens/bulk -H 'Content-Type: application/json' \
     -d '{"blob": "tok1\ntok2\ntok3"}'

# 4. 提交任务 (并发度自定)
curl -X POST localhost:8787/api/tasks -H 'Content-Type: application/json' \
     -d '{"name": "filter-01",
          "prompt_template": "对以下数据做质量过滤并打分: {{item}}",
          "items": ["data-1", "data-2"],
          "concurrency": 3000,
          "backend": "local"}'
```

## CLI

完整手册见 **`docs/CLI.md`**（含所有命令、参数、状态语义、典型工作流、故障排查，
随 CLI 变更同步更新）。速览：

```bash
codex-parallel status                                    # 总览 + 告警
codex-parallel token import-xlsx 10个gpt.xlsx            # 导入账户表
codex-parallel token list / add / add-bulk / disable / enable
codex-parallel task submit --name x --prompt '...{{item}}' --items items.txt -c 64
codex-parallel task list / show / stop / cancel / results
codex-parallel alerts [--resolve N]
```

## 失败归因策略 (attribution.py)

| 归因 | token 处理 | item 处理 |
|---|---|---|
| 账号失效 (401/403/...) | 标记 banned（永久）, 立即换新 token | 继续 |
| 5h/周/月用量满 | 对应时长冷却，到期自动回来 | 换号继续 |
| 瞬时失败 (503/网络) | 原token重试, strike+1 | 继续 |
| 3 次重试耗尽 | 丢弃该 token, 换新 | 继续 |
| 池子空/全在冷却 | — | item 失败 + CLI/Web 告警 |

## 检查点 / 沙箱重启

每个 attempt = 一条 WorkerRun 检查点 + 一个全新沙箱。沙箱内 Codex 挂掉
(崩溃/超时/换token) → finally 里 kill 旧沙箱 → 下一轮 backend.create() 开
新沙箱。心跳停滞 (默认 180s) 由 reaper 回收并重新入队。

## 多 hub 实例

SQLite WAL + `UPDATE ... WHERE status=...` 原子抢占, 多个 hub 进程可共用
一个 data 目录, 任务不重不漏。Hub 重启时 running 项自动回队列。

## 测试

```bash
pytest tests/   # 43 个: token pool / attribution / runner / checkpoint / E2E
```

E2E 用 `scripts/mock_codex.sh` 做可编程假 codex (ok/auth/transient/hang/flaky),
无需真实凭据即可验证全链路。

## backend

- `local` — 子进程沙箱 (dev/测试/小规模)
- `e2b` — 腾讯 Agent Runtime (E2B SDK), 设 `E2B_API_KEY`/`E2B_DOMAIN`,
  需要装 `pip install -e ".[e2b]"`; 参考 agentic_cl_research 项目的沙箱用法
