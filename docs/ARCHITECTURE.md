# Codex Parallel Infra — 架构设计

## 总览

统一命令中枢（Hub）以服务形式常驻部署机器，接收过滤任务，按自定义并发度把
任务项分发到 N 个沙箱内并行运行的 Codex CLI。Token 池管理认证凭据的分配、
冷却与失效标记。DB 记录一切状态。

```
                         ┌────────────────────────────────────────────┐
                         │                HUB (常驻服务)               │
                         │                                            │
  CLI ──submit/stop──►  │  ┌──────────┐   ┌──────────────────────┐   │
  Web ──submit/stop──►  │  │ FastAPI  │──►│     Scheduler        │   │
  (浏览器面板)           │  │ :8787    │   │  (asyncio 任务循环)   │   │
                         │  └────┬─────┘   └─────┬──────────┬────┘   │
                         │       │               │          │        │
                         │  ┌────▼─────┐   ┌─────▼────┐ ┌───▼─────┐  │
                         │  │  SQLite  │◄──│ TokenPool│ │Workers[]│  │
                         │  │ (状态DB) │   │ 领取/释放 │ │ 协程池   │  │
                         │  └──────────┘   └──────────┘ └───┬─────┘  │
                         └───────────────────────────────────┼────────┘
                                                             │
              ┌──────────────────────────────────────────────┼──────────────┐
              │                 Sandbox Backend (registry)   │              │
              │   local (subprocess, dev/测试)  │  e2b (腾讯 Agent Runtime) │
              └──────────────────────────────────────────────┼──────────────┘
                                                             │
        ┌───────────────────────┬────────────────────────────┼─────────┐
        │                       │                            │         │
   ┌────▼─────┐           ┌────▼─────┐                 ┌────▼─────┐    │
   │ Sandbox#1│           │ Sandbox#2│      ...        │ Sandbox#N│    │
   │ CODEX_HOME=/w/acc1   │  /w/acc2 │                 │  /w/accN │    │
   │ codex exec --json    │  同左     │                 │  同左    │    │
   │  -C /workspace       │          │                 │          │    │
   │  -o result.md        │          │                 │          │    │
   └──────────┘           └──────────┘                 └──────────┘    │
```

## 核心流程

### 1. 提交任务
```
POST /api/tasks
{
  "name": "filter-batch-01",
  "prompt_template": "对以下训练数据进行质量过滤...{{item}}",
  "items": ["<jsonl行1>", "<jsonl行2>", ...],   # 或 items_file 路径
  "concurrency": 3000,          # 沙箱+Codex 并发度（每个任务自定义）
  "codex_config": {             # 可选：model/sandbox_mode/reasoning_effort 等
    "model": "gpt-5.2",
    "sandbox_mode": "workspace-write"
  },
  "backend": "local"            # local | e2b
}
```
- Task 状态: `pending → running → paused/stopping → done / failed / cancelled`
- TaskItem 状态: `pending → assigned → running → done / failed / timeout`

### 2. Worker 生命周期（每个并发槽一个 asyncio worker 协程）
```
loop:
  1. item = scheduler.next_item(task)          # 无 item 则该 task 收尾
  2. token = token_pool.acquire(backend)       # 无可用 token → 等待(带指数退避)
  3. sandbox = backend.create(env={CODEX_HOME: workdir/..., proxy...})
  4. 在沙箱内执行 codex exec --json -C /workspace -o out.md prompt
  5. 轮询/等待完成（超时 → kill + 标记 item=timeout）
  6. 取回结果（backend 下载 out.md / events.jsonl）→ 写 DB
  7. sandbox.kill(); token_pool.release(token) → worker 健康
     └─ 检测到认证失败(401/403, exit code + stderr 特征)
        → token_pool.report_banned(token)      # 标记 banned, 不再分配
        → item 回队列重试
```

### 3. Token 池
```
tokens 表:  id | label | secret(文件路径或脱敏存储) | status | last_used_at
            cooldown_until | fail_count | created_at
status:     active | cooldown | banned | disabled
acquire():  取 status=active 且未达并发上限的 token → status 不变, in_use+1
release():  in_use-1; 若本次执行认证失败 → banned（不可逆，人工处理/替换）
补充:       POST /api/tokens 添加新 token； banned 池只读展示
```

### 4. 失败处理矩阵
| 故障             | 检测方式                    | 自动动作                     |
|------------------|-----------------------------|------------------------------|
| 沙箱起不来        | backend.create 超时/异常     | 指数退避重试 3 次 → item 失败 |
| Codex 进程挂     | exit code != 0 / 无输出      | 重试 1 次（新沙箱）→ 失败     |
| 认证失败(封号)    | stderr 含 401/403/unauthorized | token→banned, item 重试(新token) |
| 单项超时          | wall-clock 超时             | kill + item=timeout, 下一个   |
| Hub 重启         | DB 恢复 running 任务         | assigned/running 项重置回 pending |

### 5. 多 Hub 实例
- SQLite 加文件锁（WAL 模式），多进程读写在单机内安全
- 每个 Hub 实例认领 task 时用 `UPDATE ... WHERE hub_id IS NULL` 原子抢占，
  task 归属该 hub 直到完成/释放 —— 支持“中枢启动多次”

## 目录规划
```
codex_parallel_infra/
├── pyproject.toml
├── src/codex_parallel/
│   ├── __init__.py
│   ├── config.py          # 环境变量/文件配置
│   ├── db.py              # SQLAlchemy engine + session
│   ├── models.py          # Token/Task/TaskItem/WorkerRun ORM
│   ├── token_pool.py      # 领取/释放/封禁/冷却 逻辑
│   ├── backends/
│   │   ├── __init__.py    # registry: make_backend(name)
│   │   ├── base.py        # SandboxBackend Protocol
│   │   ├── local.py       # 本地子进程沙箱（dev/测试）
│   │   └── e2b.py         # 腾讯 Agent Runtime (E2B SDK)
│   ├── codex_runner.py    # 组装 codex exec 命令 + 事件解析 + 认证注入
│   ├── health.py          # worker 心跳 + 重启策略
│   ├── scheduler.py       # 任务调度循环 + 并发控制
│   ├── server.py          # FastAPI app + 静态 Web
│   └── cli.py             # 命令行入口 (typer)
├── web/index.html         # 单页面板（任务/token/worker 三视图）
├── tests/
│   ├── test_token_pool.py
│   ├── test_scheduler.py
│   ├── test_codex_runner.py
│   └── test_e2e_local.py  # local backend + mock codex 全链路
├── scripts/
│   ├── mock_codex.sh      # 测试用假 codex（可编程失败/延迟）
│   └── deploy.sh          # nohup/systemd 部署
└── docs/ARCHITECTURE.md
```

## 设计决策

1. **token 只存引用不存明文**：secret 存 `tokens/` 目录下每 token 一个文件
   （0600），DB 只存路径+指纹，API 展示脱敏 —— 便于后续从"手填 token"平滑
   演进到任何来源。
2. **backend registry 模式**：完全照搬 agentic_cl_research 的
   `make_sandbox(backend=...)` 设计，local backend 让全链路可在无云凭据时
   开发测试，e2b backend 在集群上真实并发。
3. **codex_runner 不感知 backend**：它只产出"要在一个已就绪 shell 里执行的
   命令 + 结果取回方式"，backend 负责在哪执行、怎么取回。
4. **items 是任务的最小工作单元**：一个 item = 一条训练数据 = 一次 codex exec，
   失败重试粒度是 item 级。
```
