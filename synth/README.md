# synth/ —— 合成数据生成器

> **规模不必大，但必须带真值。**
> 没有真值，你只能看曲线"像不像"；有真值，才能回答"她错在哪几个时刻、错了几次"。

## 为什么需要它

这个项目里几乎每个机制都是**统计**的：节律要学、开口要看时机、衰减要看检索。
统计的东西没法靠"读代码"验证，只能靠"喂已知答案的数据，看它答不答得出"。

所以这里生成的不是"假数据"，是**带标准答案的考题**。

## 用法

```bash
python -m synth all --days 30                    # 五个场景全跑，出 report.json
python -m synth build --scenario drift --days 60 --out var/synth/drift60
python -m synth score var/synth/drift60
python -m synth build --corpus ~/data/lccc.jsonl --out var/synth/real
```

输出目录里两样东西：

```
gits.db      走正规写入路径生成的事件库（不是伪造的表）
truth.json   真值：每段作息、每条消息的时刻
```

**关键：数据是走 `Writer` 写进去的**，所以它会经过 `ext_id` 幂等、`ts/recv_ts`、
`lag`、`ts_trust` 这一整套——验证的就是线上会跑的那套代码，不是另一条旁路。

## 五个场景

| 场景 | 造的是什么 | 用来验证 |
|---|---|---|
| `regular` | 9–6 上班，23:30 睡 | 核密度能不能收敛 |
| `night_owl` | 2 点睡到 11 点，下午到晚上在工位 | 双峰作息；均值会错，分布不会 |
| `drift` | 后半月整体推后 2 小时 | 会不会漂移过去 |
| `chaotic` | 每天随机 | **周期图该明确说"没有节律"** |
| `offline_gap` | 中间断线 3 天 | 断线=未观测，不是"他不在" |

`chaotic` 是最重要的一条：它验证的是**系统知不知道自己不知道**。
硬套一个时段表，只会让她在错误的时间开口。

## 断线补传是怎么造的

`offline_gap` 里，断线期间产生的事件先压进本地队列，重连时统一上传，
于是这些事件的 `recv_ts` 全挤在重连那一刻、`lag` 很大、`ts_trust=0`。

这正是 §14.0 要求的行为：**补传数据退出节律训练，但保留在记忆里**。
如果哪天有人把 `ts_trust` 的判断去掉，`test_断线期间是未观测而不是不在` 会立刻红。

## 评分（`synth/score.py`）

| 指标 | 含义 |
|---|---|
| `daily` / `peak_ratio` | 周期图是否检出日周期、峰/中位比 |
| `separation` | 在工位的密度 ÷ 睡觉的密度（越大越分得开） |
| `unseen_ok` | 断线期间是否返回 `None` |
| `backfill_trusted` | 补传数据里被误当成可信的条数（必须为 0） |
| `false_speak` | 睡着时还开口的比例 |

`false_speak` 在 `chaotic` 上**必然大于 0**——模型没有信息。
指标的意义在于它能看出失败，而不只是能看出成功。

## 真实语料

仓库里**不放任何第三方数据**。需要时自己下载，用 `--corpus` 指向本地 JSONL：

```jsonl
{"user": "今天开会开到想吐", "assistant": "那就别开会了"}
```

常见的几个（许可各不相同，自己看清再下）：

- **LCCC**（清华）https://github.com/thu-coai/CDial-GPT · Apache-2.0
- **豆瓣多轮** https://github.com/MarkWuNLP/MultiTurnResponseSelection · 研究用途
- **NaturalConv** https://github.com/elliott-king/NaturalConv · 研究用途

没有语料时用模板生成，够跑通机制。**模板的意义是可控，不是像人。**

## 实测规模（本机，2026-11-25）

```
365 天 · 15111 条事件 · 2.1 MB · 生成 0.3s · 重放 < 0.01s
```

比文档里估的"一年不到 200 MB"小两个数量级——因为按"只在工位采样"算，
一天约 40 条事件，不是 1000 条。**这个数应该改进 §15.8，而不是反过来改采样率。**
