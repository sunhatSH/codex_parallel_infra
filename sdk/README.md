# codex-parallel SDK

向上游服务提供的 Python SDK，封装 hub 的 REST API。`pip install ./sdk` 后：

```python
from codex_parallel_sdk import HubClient

hub = HubClient("http://hub机器:8787")   # 默认 http://127.0.0.1:8787

# ── 提交任务 ──────────────────────────────────────────────
task = hub.submit(
    items=["数据1", "数据2", "数据3"],        # 或 items_file="/path/data.txt"
    prompt="过滤以下训练数据并打分(1-10)，只输出JSON: {{item}}",
    concurrency=10,                          # 并发沙箱数
    # model="gpt-5.5",                       # 可选
    # input_files=["/path/report.pdf", "/path/data_dir/"],  # 传入沙箱，模型在 cwd 可见
)

task.wait(timeout_s=3600)                    # 阻塞到终态 (done/failed/cancelled)
for r in task.results():
    print(r["last_message"])                 # codex 的最终回复
    # r["events"] 是完整轨迹（模型每步消息/工具调用）

# ── 账户池 ────────────────────────────────────────────────
hub.token_stats()                            # {"active": N, "banned": N, ...}
hub.tokens()                                 # 明细（含冷却剩余时间 cooldown_s_left）
hub.import_xlsx("/path/10个gpt.xlsx")        # 导入账户表
hub.set_token_utilization(3, 2)              # 利用率：3号账户的 auth.json 进 2 个沙箱（默认 1）
hub.set_token_status(3, "disabled")          # 手动下线/启用

# ── 告警 ──────────────────────────────────────────────────
hub.alerts()                                 # 池耗尽等告警
hub.resolve_alert(1)

# ── 异步版 ────────────────────────────────────────────────
import asyncio
from codex_parallel_sdk import AsyncHubClient

async def main():
    async with AsyncHubClient() as hub:
        t = await hub.submit(items=["x"], prompt="p: {{item}}", concurrency=4)
        await t.wait()
        rs = await t.results()

asyncio.run(main())
```

## 方法一览

| 方法 | 说明 |
|---|---|
| `health()` | hub 存活 |
| `submit(items=…/items_file=…, prompt=…, concurrency=…, input_files=…)` | 提交任务，返回 TaskHandle |
| `task(id)` / `tasks()` | 取已有任务句柄 / 列表 |
| `task.wait(timeout_s)` / `.status()` / `.results()` / `.stop()` / `.cancel()` | 任务生命周期 |
| `token_stats()` / `tokens()` | 池状态 |
| `add_token` / `add_tokens_bulk` / `import_xlsx` | 加账户 |
| `set_token_utilization(id, n)` | **利用率**（一个账户进几个沙箱，默认 1） |
| `set_token_status(id, "active"/"disabled")` | 手动启停 |
| `alerts()` / `resolve_alert(id)` | 池告警 |

## 结果与轨迹数据位置

- SDK 途径：`task.results()` → `[{last_message, exit_code, events(完整codex轨迹)}]`
- 磁盘：hub 机器 `data/results/<task_id>/item-<id>.json`
- 审计：DB `worker_runs` 表（每次尝试的账户/沙箱/结局）

## 并发与利用率的关系

实际并发 = min(任务 `concurrency`, Σ 各账户 `max_concurrency`)。
默认每账户 1：10 个账户 → 最大 10 并发。要更多并发要么加账户，要么调利用率
（chatgpt 账户 RT 会轮转，利用率 >1 有互踢风险，见 CLI 手册）。
