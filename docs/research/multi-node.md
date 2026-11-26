# 多端调度：频率 × 硬件 × 同步节律

> 上一版的纪律是"`ghost` 只许 numpy"，那是从一次测量（statsmodels 152 MB）
> 直接推出来的**禁令**。禁令太硬：它把"重计算、重依赖、甚至 GPU 和机器学习"
> 一刀切掉了。
>
> 正确的形式是一个**预算问题**：任务有多重、多久跑一次、数据要多新，
> 机器有多少余量、多久能同步一次。四条一对，位置就定了。

## 1. 节点画像（实测）

| | cloud（ECS） | office（工作机） |
|---|---|---|
| CPU | 2 | 12 |
| RAM | 1.6 GB | 30 GB |
| 可用磁盘 | 30 GB | 638 GB |
| GPU | — | RTX 3050 **6 GB** |
| 网络 | 国内 | 国内 + proxy |
| 常驻占用 | ~250 MB（系统 + murmurd） | ~600 MB |
| **后台作业预算** | **500 MB** | **8 GB** |
| 测试 venv 安装 | numpy+statsmodels = **361 MB 磁盘 / 31 s** | 不计（盘大） |
| 同步周期 | 1 分钟（它就是源头） | **30 分钟**（`s_node` 高时才同步） |

一句话：**ECS 稳定但瘦，PC 胖但会走。**

## 2. 任务画像（频率决定位置）

| 任务 | 频率 | 内存峰值 | 能容忍多旧 | 天然位置 |
|---|---|---|---|---|
| 路由改写（压到 25 字） | 每次开口 | 20 MB | 10 min | **cloud** |
| 抽取（对话 → 节点） | 每条消息 | 60 MB | 1 h | cloud（延迟敏感） |
| embedding | 每条新节点 | **900 MB**（bge-m3） | 1 h | **office**（GPU） |
| 夜间巩固 / 聚类 | 每天 1 次 | 300 MB | 6 h | office 优先，cloud 兜底 |
| 自我叙事 | 每天 1–3 次 | 150 MB | 6 h | cloud（要长上下文） |
| 声学特征 | 每 10 分钟 | 40 MB | 0（必须本地） | **office**（麦克风在那） |
| 企划中的小模型推理 | 每周 | 4–6 GB | 1 天 | office（显存 6 GB） |

**规律**：延迟敏感 + 内存小 → 云；内存大 + 有 GPU → 工作机；
依赖数据新鲜度 → 只能云。

## 3. 三条硬约束（`gits/sched.py`）

```python
eligible(node, task, health) -> (bool, reason)
  1. 能力：task.ram_mb ≤ node.headroom_mb()，且显存够
  2. 新鲜度：task.staleness_max_ms ≥ node.sync_period_ms
  3. 时机：phase ∉ {down, draining}
```

第 2 条最容易被忽略，也最容易出事：

> 一个"只能忍 5 分钟"的任务派给 30 分钟才同步一次的工作机，
> **它拿到的就是旧数据**——算得再快也是错的。

所以"新鲜度"必须写进任务画像，而不是事后祈祷。

## 4. 多端时序状态机

每台机器五个状态，由**心跳、新鲜度、租约**三者决定，模型不参与：

```
         心跳超时
  ┌──────────────────────► DOWN
  │                          │ 心跳回来
  │                          ▼
IDLE ◄──── 任务完成 ──── BUSY ──── 要走了 ──► DRAINING ──► DOWN
  │                        ▲
  └── 数据过期 ──► STALE ───┘（同步完就回 IDLE）
```

| 状态 | 进入条件 | 允许 |
|---|---|---|
| `DOWN` | 心跳年龄 > 3 分钟 | 什么都不派 |
| `STALE` | 新鲜度 > 2 h | **只读**，结果不许提交 |
| `IDLE` | 活着且数据新 | 领任务 |
| `BUSY` | 持有租约 | 干活，续租 |
| `DRAINING` | 人回来了 / 要关机 | 只收尾、只提交 |

**DRAINING 是最重要的一格**：它给了"我现在要用电脑"一个体面的收尾窗口，
而不是一刀 kill 掉跑到一半的任务。没有它，后台作业迟早会写坏一次半成品。

## 5. 同步节律也是调度的输入

工作机的同步**不是定时任务**，是 `s_node`（人不在键鼠前）驱动的。
于是数据新鲜度天然跟着作息走：

```
你离开键盘 30 分钟 → s_node 高 → 同步 + 派活
你一回来        → DRAINING  → 收尾提交，不接新活
```

这条把 §14 的节律模型和 §15 的作业队列接上了：**同一张 `s_node` 分布，
既决定"什么时候能算"，也决定"数据有多新"。**

## 6. 依赖预算，而不是依赖禁令

原来的规则"ghost 只许 numpy"改成**预算**。体积都是实测的
（`uv pip install` 之后 `du -sm`，不是 wheel 大小——wheel 要展开 3–10 倍）：

| 节点 / 层 | 依赖 | 磁盘 | 常驻 RSS | 预算 | 余量 |
|---|---|---|---|---|---|
| cloud / ghost | numpy | **53 MB** | ~30 MB | 无上限 | stdlib+numpy |
| cloud / dream | + statsmodels | **260 MB** | 152 MB | 400 MB | 248 MB |
| office / ml | + onnxruntime | **128 MB** | 待测（估 150） | 8 GB | 很大 |

**放得进去就允许**，但必须有实测数字。禁令退化成一条：
**ghost 只允许 stdlib + numpy。** RSS < 200 MB、import < 500 ms 是观测指标，
不是允许清单（早先写成「90 MB 预算」，那是倒推出来的）。这条有测试守着
（`test_ghost_不导入_statsmodels`）。

⚠️ 上一版我写"ECS 装不下 ml：onnxruntime 200 MB、torch 2 GB+"——**实测纠正**：
onnxruntime 装上只有 **128 MB**，ECS 的 31 GB 盘**放得下**。
真正的门槛不是磁盘也不是内存，是**依赖纪律**：ghost 只允许 stdlib + numpy，
所以它只能进 dream 或 office。**torch 那 2 GB 量级的东西才是真的上不了云。**

## 7. uv 怎么管

```toml
[project]
requires-python = ">=3.10"        # ECS 是 3.10.12，锁死下限

[project.optional-dependencies]
ghost = ["numpy>=1.24"]                        # cloud 常驻
dream = ["numpy>=1.24", "statsmodels>=0.14"]   # cloud 夜间
ml    = ["numpy>=1.24", "onnxruntime>=1.18"]   # office 的 GPU 活
dev   = ["pytest>=8"]
```

- **`uv.lock` 入库**（262 KB）：两台机器的 Python 版本不同（3.10 / 3.14），
  `uv` 出的是跨版本通用锁；锁了才能保证"云上跑的和本机跑的是同一套"。
- 每台机器只装自己那层：`uv sync --extra ghost --extra dream`（云）/
  `uv sync --extra ghost --extra ml --extra dev`（PC）。
- **装之前先看数字**：`du -sm .venv`、启动后 `VmRSS` 各量一次，写回本表。
  没有数字的依赖不许进 ghost。
- **磁盘代价**要算进去：ECS 40 GB 里已经用掉 6.6 GB，装 ml 那套（onnxruntime
  约 200 MB、torch 会到 2 GB+）**放不下**，所以 ml 只在 office。

## 8. 一句话

> 位置不是选出来的，是**算出来的**：任务有多重、多久跑一次、数据要多新，
> 机器有多少余量、多久能同步一次。四条一对，答案唯一——
> 唯一剩下的自由度是"没有机器能满足时，宁可等"。**等是合法的调度结果。**
