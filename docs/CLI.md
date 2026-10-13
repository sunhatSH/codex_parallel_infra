# codex-parallel CLI 使用手册

> 本手册与 CLI 同步维护：命令有增删改时必须更新此文件。
> 最后更新：2026-10-13（对应 dev `93cf412`，77 tests）

## 安装与调用

```bash
pip install -e .                      # 装好后二进制叫 codex-parallel
codex-parallel --help                 # 安装后的调用方式
python -m codex_parallel_cli --help   # 仓库内等价调用（无需安装步骤）
```

CLI 是纯 HTTP 客户端，**可以在任意机器用**（笔记本/跳板机），只需能连通 hub：

```bash
codex-parallel --url http://<hub机器>:8787 status        # 全局 --url 覆盖默认地址
export CODEX_PARALLEL_URL=http://hub:8787                # 或用环境变量（每次都带 --url 太烦时）
```

默认地址 `http://127.0.0.1:8787`。

## 命令总览

```
codex-parallel serve      启动 hub 服务（部署机上跑）
codex-parallel status     总览：hub/token 池/任务 + 活跃告警
codex-parallel alerts     告警列表 / 解决告警
codex-parallel token …    账户池管理
codex-parallel task …     任务管理
```

---

## serve — 启动服务

```bash
codex-parallel serve --port 8787 --host 0.0.0.0
codex-parallel serve --home /data/codex-hub     # 指定数据目录（DB/secrets/结果）
```

阻塞式运行，部署机上一律放 tmux：

```bash
bash scripts/start-all.sh start    # 推荐：一键起 hub + web 面板（tmux）
bash scripts/start-all.sh stop|status|restart
```

环境变量（serve 前设好）：

| 变量 | 作用 |
|---|---|
| `E2B_API_KEY` / `E2B_DOMAIN` | e2b backend（腾讯 Agent Runtime）必需，`ap-beijing.tencentags.com` |
| `E2B_TEMPLATE` | 沙箱模板名，默认 `codex-parallel-sandbox` |
| `CODEX_PARALLEL_HOME` | 数据目录，同 `--home` |

## status — 总览

```bash
codex-parallel status
```

输出 hub 状态、token 池统计（active/banned/discarded/in-use）、任务数。
**池耗尽时 stderr 打红色告警** + 提示补充命令。

## alerts — 告警

```bash
codex-parallel alerts                  # 列出活跃告警（池耗尽等）
codex-parallel alerts --resolve 3      # 解决 3 号告警（补完 token 后）
```

---

## token — 账户池

### import-xlsx —— 导入账户表（主要入口）

```bash
codex-parallel token import-xlsx 10个gpt.xlsx
codex-parallel token import-xlsx /path/to/账.xlsx --no-exchange
```

- xlsx 在 **hub 机器**上（web 上传走的是 base64 blob，CLI 走本地路径）
- 自动解析：A 列 `账号----密码` 做 label，P 列 token bundle（RT/access_token/account_id）转成 auth.json 存入
- access_token 未过期 → 直接构造，不联网；过期/缺失 → 用 RT 走 auth.openai.com 兑换（`--no-exchange` 跳过）
- 重复导入自动跳过（按 label 判重），一行一账户，secret 0600 文件，token↔auth.json 1:1

### add / add-bulk —— 手动加

```bash
codex-parallel token add my-acc --secret sk-xxx          # 单个（API key 或 RT）
codex-parallel token add my-acc --secret - < secret.txt  # 从 stdin 读，避免进 shell 历史
codex-parallel token add-bulk tokens.txt                 # 每行一个
cat tokens.txt | codex-parallel token add-bulk - --label-prefix batch2
codex-parallel token add-bulk tokens.txt --max-concurrency 2   # 利用率：auth.json 进 2 个沙箱
```

### list / enable / disable

```bash
codex-parallel token list                # 表格：状态/并发/失败/冷却/原因
codex-parallel token list --raw          # 原始 JSON（含 quota_window、cooldown_s_left）
codex-parallel token disable 5           # 手动下线 5 号（如预知要维护）
codex-parallel token enable 5            # 恢复
```

### 状态含义

| status | 含义 | 自动恢复？ |
|---|---|---|
| `active` | 可用（含冷却中，看 cooldown 列） | 冷却到期自动回来 |
| `banned` | 账户被封/凭据失效，**永久** | 否，需换新号 |
| `discarded` | 3 次重试耗尽被丢弃 | 否 |
| `disabled` | 手动下线 | 手动 enable |

冷却来源：5h/周/月用量窗口，到期自动释放，无需人工。

### 利用率（并发）

默认 1（一个账户同时只在一个沙箱）。Web 面板可改；CLI 暂无命令，走 API：

```bash
curl -X POST http://127.0.0.1:8787/api/tokens/5/concurrency \
     -H 'Content-Type: application/json' -d '{"max_concurrency": 2}'
```

> chatgpt 账户（RT 轮转）建议保持 1：多沙箱并发同账户会互踢登录态。

---

## task — 任务

### submit

```bash
codex-parallel task submit \
    --name filter-batch-01 \
    --prompt '对以下训练数据做质量过滤并打分(1-10)，只输出JSON: {{item}}' \
    --items items.txt \
    --concurrency 10 \
    --backend local \
    --model gpt-5.2
```

| 参数 | 说明 |
|---|---|
| `--name` | 任务名 |
| `--prompt` | 模板，**必须含 `{{item}}`**，每个 item 替换后发给一个 codex |
| `--items` | 文件，每行一个 item |
| `--concurrency / -c` | 并发沙箱数（上限 = 可用账户数 × 利用率） |
| `--backend` | `local`（默认）/ `e2b`（腾讯沙箱） |
| `--model` | 传给 codex 的 `-m` |

### 查看/控制

```bash
codex-parallel task list                 # 全部任务 + 进度
codex-parallel task list --raw
codex-parallel task show 3               # 3 号任务每个 item 的状态/耗时/错误
codex-parallel task show 3 --raw
codex-parallel task results 3            # 已完成 item 的结果（last_message 等）
codex-parallel task stop 3               # 优雅停（跑完手头的，不再领新 item）
codex-parallel task cancel 3             # 立即取消（未开始的 item 全部 cancelled）
```

### item 状态

`pending → assigned → running → done / failed / timeout / cancelled`
失败自动重试规则见 `docs/`：账户死→换号；瞬时错→同号 3 次；用量满→冷却换号；沙箱挂→检查点重开。

---

## 典型工作流

```bash
# 0. 部署机起服务
bash scripts/start-all.sh start

# 1. 导入账户
codex-parallel token import-xlsx 10个gpt.xlsx

# 2. 看池子
codex-parallel token list

# 3. 提交任务（10 个账户 → 并发 10）
codex-parallel task submit --name w1 --prompt '过滤: {{item}}' --items data.txt -c 10

# 4. 盯进度
codex-parallel status
codex-parallel task show 1

# 5. 拿结果
codex-parallel task results 1 --raw > results.json

# 6. 池耗尽告警出现时：补号 → 消警
codex-parallel token import-xlsx 新一批.xlsx
codex-parallel alerts --resolve 1
```

## Web 面板

`http://<hub>:5173/`（与 CLI 同一 API）：任务提交/进度条、token 池管理（xlsx 上传导入、利用率调整、冷却显示）、池告警红条。CLI 和 Web 可混用，操作的都是同一个 hub。

## 故障排查

| 症状 | 检查 |
|---|---|
| `Hub 不可达` | hub 起没起（`tmux ls` 看有没有 codex-hub）；`--url` 对不对 |
| `no tokens available` | `token list` 看是不是全 banned/冷却中；补号或等冷却 |
| item 全 timeout | 沙箱出不了 OpenAI 的网（北京出口被封），见 `docker/sandbox/BUILD.md` 网络节 |
| e2b 起沙箱报错 | `E2B_API_KEY/DOMAIN` 设了没；AGS key 是 `ark_` 前缀（已自动处理校验） |
