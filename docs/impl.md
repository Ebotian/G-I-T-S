# 实现方案 v0

创建：2026-11-25　｜　状态：草案

---

## 1. 三条判据

选型不用讨论"哪个技术更好"，只问三个问题：

1. **性能卡在哪？** ——不在我们手里。
2. **开发效率卡在哪？** ——在"改一次设计要动多少代码"。
3. **什么不该自己写？** ——凡是会替我们决定 prompt 或 schema 的东西。

第 1 条的答案是：**端到端 1.7 秒里，1.4 秒在 DeepSeek 那边**。我们的代码跑多快，对体验几乎没影响。所以任何"为了性能换语言/上框架"的提议，都应该先被这一条否掉。

第 3 条最容易被忽视，但它决定了三个拒绝：**不用 LangChain、不用 ORM、不给模型 tool**。

---

## 2. 性能预算

### 2.1 延迟（单轮，p50）

| 段 | 耗时 | 谁决定 |
|---|---|---|
| 微信长轮询送达 | **0–35 s** | 微信（不可控，最大抖动源） |
| 事件入库 + 检索 | 20 ms | 本地 |
| 上下文装配 | 5 ms | 本地（A 段有缓存） |
| LLM 首 token | 600–900 ms | DeepSeek |
| 流式生成（80 字） | ~800 ms | DeepSeek |
| 路由 + 可选改写 | 0–400 ms | 本地 + 一次小调用 |
| 投递 | ~100 ms | 微信 |

**关键动作**：收到消息立刻发 `sendtyping`（正在输入）再开始思考，把 1.7 秒藏进对方的"她正在打字"里。这是唯一能吃掉延迟的手段。

### 2.2 内存（ECS，1.6 GiB）

| 进程 | 目标 RSS |
|---|---|
| 系统 + murmurd + 阿里云 agent | ~250 MB |
| `ghost` | 90 MB |
| `shell` | 70 MB |
| `dream`（空闲） | 60 MB |
| **合计** | **~470 MB**（余量 ~500 MB，必须有 swap 兜底） |

每个 systemd 单元设 `MemoryHigh` / `MemoryMax`，让 OOM 在单元级别发生，而不是把整台机器带走。

### 2.3 容量

一天约 1000 条事件 → 一年 36 万行 → SQLite 几十 MB。**容量永远不是问题**，问题只会是"某个查询没走索引"。

---

## 3. 技术栈

| 层 | 选 | 不选 | 理由 |
|---|---|---|---|
| 语言 | **Python 3.10**（下限） | Rust / Go | 性能不在我们手里；生态（jieba/numpy/scipy/sqlite3）在 Python |
| 环境 | `uv` | poetry / conda | 已在用，且能一键装 3.10 做版本对齐 |
| HTTP 客户端 | **httpx** | openai-sdk | 我们需要 DeepSeek 特有字段（`safety_check`/`thinking`）、精确的用量记账、以及同一个客户端跑作业队列 |
| HTTP 服务端 | **aiohttp** | FastAPI / Starlette | 单包、无 ASGI 仪式、无 pydantic。内存 ~15 MB |
| 数据库 | **sqlite3（stdlib）** | SQLAlchemy / Alembic | 开发期 schema 天天改，ORM 会挡路（§schema §6） |
| 序列化 | **cbor2** | JSON | 声学特征卡是有数组的，JSON 编码又胖又慢 |
| 数值 | **numpy + scipy** | — | `scipy.signal.lombscargle` 直接可用（§rhythm） |
| 分词 | **jieba** | — | 中文 FTS5 的必需品 |
| 调度 | **asyncio + 五分钟 tick** | APScheduler / Celery | 我们要的是"按节律评估"，不是 cron |
| 进程管理 | **systemd** | docker / k8s | 1.6 G 上给容器预留内存是浪费 |
| Mumble | **pymumble**（GPL-3.0，可选） | 自己实现协议 | **不在关键路径上**（§5）。出声用 `pw-play` 就够 |
| 向量 | **sqlite-vec**（<1.0） | 起一个向量库 | 先不建，等 embedding 跑通（schema §4） |
| 测试 | **pytest** | — | 核心是重放测试 |

**明确不选的四样，理由都是"它会替我们决定不该它决定的事"**：

- **LangChain / LlamaIndex**：它们拥有 prompt 的组装权，而 prompt 就是人格（§5.4）。用它们等于把她的声音外包。
- **ORM + 自动迁移**：schema 是设计的一部分，不能交给一个"看不懂语义"的工具去 diff。
- **给模型任何 tool**：一旦她手里有工具箱，她就退回助手（§10.3-2）。所有"能力"都由确定性代码在生成之后施加。
- **向量数据库服务**：单用户、10⁵ 量级，SQLite 一个扩展就够。

---

## 4. 代码结构

```
gits/
  cli.py          150   run / status / replay / rebuild / review
  config.py        80   TOML，全部魔法数字住在这里
  db.py           180   连接、WAL、迁移、★单写者线程
  events.py       120   追加、游标、重放
  llm.py          160   httpx + SSE + 用量记账 + 缓存命中统计
  context.py      150   A/B/C 装配（§5.4）
  persona.py      100   自我叙事（§5.5）
  memory.py       400   抽取、检索、衰减（§5.1）
  rhythm.py       250   lombscargle / 周期核密度 / BOCPD / hazard（§14）
  gate.py         120   开口价值（§5.3）
  social.py       200   attitudes / rituals / EE（§13）
  route.py        100   运动皮层（§5.6）
  worker.py       150   拉取式作业（§15.4）
  node.py          80   心跳、idle、能力上报
  channels/
    wechat.py     350   iLink：长轮询、context_token、SILK
    office.py     150   声学采集 + 本地播放
```

**合计 ~2400 行。** 这不是一个十万行的项目，这也是为什么"先把 events 和重放做对"是可行的——它的成本只有 300 行。

### 4.1 一个必须从一开始就做对的细节：单写者

SQLite 的并发模型是"多读单写"。做法不是加锁，而是**结构上只有一个写入者**：

```
所有进程 → asyncio.Queue → writer 协程 → 唯一的写连接
```

好处：不可能出现 `database is locked`，写入顺序确定，重放可复现。代价是 ~40 行。**这个必须在 P0 就落地**，后期补会很痛。

---

## 5. 从开源拿什么、不拿什么

| 项目 | 拿什么 | 注意 |
|---|---|---|
| **Hindsight** | 后台/积压的状态模型（§15.7） | 我们只要那个"未处理是可报告状态"的思路，不接它的服务 |
| **nocturne_memory** | 版本链（Node/Memory 分离）、changeset 审计 | 不接它的 MCP 交互模式（§12.1） |
| **revive-companion** | 三阶段分解、`reason` 字符串、`0.85^n` 衰减 | 不装它的包；它的"信息增益"是手写标量（§12.3） |
| **pymumble**（可选） | Mumble 客户端 | ⚠️ **GPL-3.0**（与 AGPL-3.0 兼容），但 **Snyk 记录其已 12 个月无新版、维护不活跃**。→ 既然出声直接走本机播放，**默认不装**；将来需要多听众再隔离进 `channels/mumble.py` |
| **sqlite-vec** | FTS5 之外的向量召回 | **<1.0**，pre-release。→ 先不引入，等 embedding 跑通再决定 |
| **AstrBot / OpenClaw** | 无 | 两者都自带 agent 循环，会与 §10.3-2 冲突 |

**P4 不需要 Mumble，这不是降级路径，是默认路径。** `shell@office` 收到音频直接 `pw-play` 到当前默认 sink。Mumble 只为"多个听众"和"你不在桌边但 PC 还开着"存在，将来需要再说。别让一个 GPL 的停滞库卡住整条链路。

---

## 6. 六个实现风险

| 风险 | 概率 | 对策 |
|---|---|---|
| iLink 协议是非正式文档 | 高 | `channels/wechat.py` 单文件隔离 + 事件层与通道层解耦；协议变了只换这一个文件 |
| Python 3.10（ECS）vs 3.14（本机） | 中 | 用 `uv venv --python 3.10` 做对齐；CI 只跑 3.10；禁用 `TaskGroup` / `except*` 等 3.11+ 语法 |
| `safety_check` 误杀亲密语境 | 中 | 先开着跑两周，记录被拦的比例；如果误杀高，改成"只审输出不审输入" |
| 微信语音条 SILK | 中 | 先发**文件附件**（不用转码），等有时间再做 SILK 编码器 |
| DeepSeek 接口变动 | 中 | `llm.py` 一个适配层，参数与模型名都在 config |
| 后台 worker 让你卡顿一次 → 你永久关掉它 | **高** | 优先级压狠（CPUWeight=10）、GPU 利用率 >20% 就退、`gits status` 一眼能看到它在干什么 |

---

## 7. P0 的验收标准

不是"能跑"，是这四条可执行的判据：

1. `gits replay --from 0` 从空库重放 **一小时的真实事件**，两次运行得到**字节级相同的** `nodes`/`edges`（允许 id 不同，其余一致）；
2. 故意 `kill -9` 写入进程，重启后 `events` 无重复、无丢失；
3. `gits status` 一行能说清：节点在线、积压深度、`s_user`、`reciprocity`、`backlog`；
4. **A 段的哈希在一整天内不变**（缓存纪律的自动测试，§5.4）。

第 4 条看起来最无关紧要，其实最贵——它是缓存命中率 50 倍价差的守门人。

---

## 8. 不做什么（拒绝清单）

- 不做 Web 界面。`gits status` + `gits review` 两个 CLI 足够。
- 不做多用户。所有 `WHERE user_id` 都是冗余。
- 不做插件系统。她是给一个人用的。
- 不做 docker 部署。
- 不写 docs 之外的设计文档。这份 + `schema.md` 就够，剩下的写在代码注释里。
