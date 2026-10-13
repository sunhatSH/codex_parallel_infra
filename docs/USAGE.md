# Codex Parallel Hub — 使用总览

> 三种使用方式：**CLI**（运维/手动）、**Web 面板**（可视化）、**SDK**（程序接入）。
> 三者操作同一个 hub、同一个数据库，可任意混用。
> 更新时间：2026-10-13（dev `0d2baab`，98 tests）

## 系统是什么

一个"命令中枢"服务：你给它一批数据和 prompt 模板，它把每条数据分发给
沙箱里的 Codex CLI（真实 ChatGPT 账户驱动）并行处理，自动处理账号轮换、
封号换号、用量冷却、挂掉重试，最后把每条的结果+完整轨迹交回给你。

```
你 ──CLI/Web/SDK──► Hub (FastAPI :8787 + SQLite)
                        │  账户池（token↔auth.json 1:1，RT 自动轮转续存）
                        ▼
              N 个沙箱 × Codex CLI（并发自定义，可到千级）
                        │  失败归因：封号→换号 / 用量满→冷却 / 挂了→检查点重试
                        ▼
              data/results/<task_id>/item-<id>.json（回复+轨迹）
```

## 启动（一次性，部署机）

```bash
bash scripts/start-all.sh start     # tmux 起 hub(:8787) + web(:5173)
bash scripts/start-all.sh status|stop|restart
```

账户导入后即可干活。hub 重启状态不丢（SQLite 持久化，跑一半的任务自动续）。

---

## 方式一：CLI（运维 / 脚本）

完整手册 `docs/CLI.md`。核心命令：

```bash
# 导入账户（xlsx：A列账号密码做label，P列token bundle）
codex-parallel token import-xlsx 10个gpt.xlsx

# 提交任务（items 文件是位置参数；name 默认取文件名；-f 随任务传文件进沙箱）
codex-parallel task submit data.txt -c 10
codex-parallel task submit data.txt -f report.pdf -f data_dir/ --name 批次2

# 盯 + 取
codex-parallel status                 # 总览（池告警会打红字）
codex-parallel task show 5            # 5 号任务逐条进度
codex-parallel task results 5 --raw > out.json

# 控制
codex-parallel task stop 5            # 优雅停     task cancel 5 立即停
codex-parallel token list             # 池状态（含冷却倒计时）
codex-parallel alerts --resolve 1     # 池告警消除
```

利用率（一个账户的 auth.json 进几个沙箱，默认 1）CLI 走 API：
`curl -X POST :8787/api/tokens/3/concurrency -d '{"max_concurrency":2}'`

## 方式二：Web 面板（可视化）

浏览器开 `http://<hub机>:5173/`（React，`web-react/`，独立于后端部署）。

- **任务页**：填表提交（name/prompt/items/并发度）、进度条、点行看每个 item
  的状态/耗时/错误、stop 按钮
- **Token 池页**：xlsx 直接上传导入、每行"改"按钮调利用率、禁用/启用、
  冷却倒计时（`weekly 6d23h`）、封禁原因展示
- **顶部红条**：池耗尽/全冷却告警，可点"知道了"消除
- 5 秒自动刷新，可与 CLI/SDK 同时操作

## 方式三：SDK（程序接入）

`pip install ./sdk`（或把 `sdk/codex_parallel_sdk` 拷进你的工程）。详读 `sdk/README.md`：

```python
from codex_parallel_sdk import HubClient

hub = HubClient("http://hub机器:8787")

task = hub.submit(
    items=["数据1", "数据2"],                       # 或 items_file="data.txt"
    prompt="过滤以下训练数据并打分(1-10)，只输出JSON: {{item}}",
    concurrency=10,
    input_files=["report.pdf", "data_dir/"],        # 文件传入沙箱（原始目录结构）
)
task.wait(timeout_s=3600)                            # 阻塞到终态
for r in task.results():
    print(r["last_message"])                        # codex 最终回复
    # r["events"] = 完整轨迹（每步消息/工具调用）

hub.token_stats()                                    # {"active": 9, ...}
hub.set_token_utilization(3, 2)                      # 利用率（默认1）
hub.import_xlsx("/path/新账户.xlsx")                  # 程序化补号
hub.alerts()                                         # 池告警
```

异步版：`AsyncHubClient`，同一套方法加 `await`。

---

## 三种方式对照

| 能力 | CLI | Web | SDK |
|---|---|---|---|
| 提交任务（指定并发） | ✅ `-c N` | ✅ 表单 | ✅ `concurrency=N` |
| 传文件进沙箱 | ✅ `-f` | ❌（用 CLI/SDK） | ✅ `input_files` |
| 指定利用率 | curl API | ✅ 改按钮 | ✅ `set_token_utilization` |
| xlsx 导入 | ✅ | ✅ 上传 | ✅ `import_xlsx` |
| 看进度/结果 | ✅ | ✅ 进度条 | ✅ `wait/results` |
| 池告警 | ✅ 红字 | ✅ 红条 | ✅ `alerts()` |
| 适合 | 运维、cron、排障 | 日常盯盘 | 训练 pipeline、自动化 |

## 结果与轨迹位置（三方式通用）

| 什么 | 在哪 |
|---|---|
| 每条数据的回复+轨迹 | `data/results/<task_id>/item-<id>.json`（`last_message` + `events`） |
| SDK 取 | `task.results()` 直接拿到上面内容 |
| CLI 取 | `codex-parallel task results <id> --raw` |
| 每次尝试的审计（账户/沙箱/结局） | DB `worker_runs` 表 |
| 账户状态（冷却/封禁/RT） | DB `tokens` 表 + `data/secrets/*.secret`（0600） |

## 账户池行为（三方式共享，全自动）

| 情况 | 处理 |
|---|---|
| 账户被封（401/403） | 立即永久 banned，换下一个账户继续 |
| 用量满（周/月窗口） | 冷却对应时长，到期自动回来；5h 窗口该套餐未出现，按接口返回动态适配 |
| 每次任务后查余额 | 剩余 <2%（1/50）提前冷却，不等 429 |
| codex/沙箱挂了 | 检查点重试：销毁沙箱→重开→同账户3次→换账户 |
| RT 轮转 | 每次运行后从沙箱读回新 RT 立即落库；兑换缓存防止重复烧 RT |
| 池空/全冷却 | item 失败 + CLI 红字/Web 红条/SDK alerts() 三端告警 |

## 故障排查速查

| 症状 | 检查 |
|---|---|
| Hub 不可达 | `tmux ls` 有没有 codex-hub；`--url`/SDK 地址对不对 |
| no tokens available | `token list` 看是否全 banned/冷却；补号或等冷却 |
| item 全 timeout | 沙箱侧出不了 OpenAI（北京出口），需走代理或换区域 |
| 模型 400 not supported | 用 `-m gpt-5.5`（prolite 账户验证过；默认已带） |
