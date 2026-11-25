# synth/ —— 合成数据生成器

> **规模不必大，但必须从统计规律出发、并且带真值。**
>
> 造一个"每天 9:00 整开始、每 10 分钟一条、零抖动"的数据，然后拿它去验证
> 核密度和周期图——那不是验证，那是自我安慰。

## 生成的是什么

每个数都是**采样**出来的，不是把时刻表抄一遍：

| 量 | 分布 |
|---|---|
| 起床 | `Normal(7:30, 40min)`，周末再往后漂 |
| 上班时长 | `Normal(8h, 1.2h)`，截断到 [2.5h, 11h] |
| 午休 | 开始 `Uniform`，时长 `Normal(1h, 20min)` |
| 在岗/离开 | 两状态连续时间马尔可夫链，驻留 `Exponential` |
| 消息 | 按状态的泊松到达 → **成簇**（一次会话 1..6 条，间隔 `Exponential(45s)`，12% 概率插入长停顿） |
| 漂移切换点 | `Uniform(35%, 75%)`，不是正好一半 |
| 断线 | 起点与长度都采样 |

真值记录的是**这一次采样的实际结果**；参数另存一份，用来检查"学到的东西是否收敛到参数"。

## 为什么必须这样

初版用的是干净模板，实测 `peak_ratio` 冲到 **1250**——真实人类活动永远不会有这种周期性。
这样的数据能验证"代码跑得通"，但验证不了"统计机制对不对"。

改成采样之后：

```
regular    peak_ratio 130   separation 13.0   daily ✓
night_owl  peak_ratio 184   separation 15.8   daily ✓
drift      peak_ratio 120   separation 11.8   daily ✓
chaotic    peak_ratio  11   separation  1.2   daily ✗   false_speak 25.5%
```

`chaotic` 那一行的 `false_speak` 是**故意保留的失败**：模型没有信息，
误开口率必然高。指标的意义在于它能看出失败。

## 用法

```bash
python -m synth all --days 30                    # 五个场景 + 打分
python -m synth build --scenario drift --days 60 --out var/synth/drift60
python -m synth score var/synth/drift60
python -m synth build --corpus ~/data/lccc.jsonl --out var/synth/real
```

输出两样东西：`gits.db`（走正规 `Writer` 写入的事件库）+ `truth.json`（真值）。

**数据走正规写入路径**，所以会经过 `ext_id` 幂等、`ts/recv_ts`、`lag`、
`ts_trust` 这一整套——验证的就是线上会跑的代码。

## 五个场景

| 场景 | 造的是什么 | 验证 |
|---|---|---|
| `regular` | 作息宽约 ±40min 抖动 | 核密度能否收敛 |
| `night_owl` | 2 点睡到 11 点 | 双峰；均值会错，分布不会 |
| `drift` | 中途整体推后 2 小时（切换点采样） | 会不会漂移过去 |
| `chaotic` | 连续时间随机游走，**不在零点重置** | 该明确说"没有节律" |
| `offline_gap` | 随机起止的断线 2–5 天 | 断线=未观测，不是"他不在" |

`chaotic` 的"不在零点重置"是踩出来的坑：每天零点重新采样会让零点变成
一个真实的边界，周期图于是在 24h 上误报。

## 一个更根本的坑：周期检验的零分布

判断"有没有日周期"时，我先后试了三种零分布：

| 零分布 | 结果 |
|---|---|
| 均匀重撒 | 把成簇抹平，方差被低估 → **误报**（chaotic 10 个种子里 4 个判成有节律） |
| 环形平移 | 对纯周期序列，整体平移不改变相位差、周期图相位不变 → **真节律反而检不出** |
| **间隔自助** | 从观测到的间隔里有放回抽，重建时间轴：保留重尾与成簇，只破坏"每天同一时刻" |

用间隔自助之后，10 个种子：

```
regular    TTTTTTTTTT
night_owl  TTTTTTTTTT
drift      TTTTTTTTTT
chaotic    FFFFFFFFFF
```

## 评分（`synth/score.py`）

| 指标 | 含义 |
|---|---|
| `daily` / `peak_ratio` | 周期检验结论、峰/中位比 |
| `separation` | 在工位密度 ÷ 睡觉密度（越大越分得开） |
| `unseen_ok` | 断线期间是否返回 `None` |
| `backfill_trusted` | 补传数据里被误当成可信的条数（必须 0） |
| `false_speak` | 睡着时还开口的比例 |

## 验证生成器本身

`tests/test_generator.py` 检查的不是系统，是**考题**：

- 每天消息量过度离散（`var/mean > 1.5`），证明成簇
- 间隔 CV `> 1.5` 且 2% 以上超过半小时，证明重尾
- 起床时刻 `std > 0.3h`，证明不是常数
- `chaotic` 的每小时在场分布 `max/mean < 3`，`regular` 则 `> 3`

**生成器不写测试，等于用不可信的东西验证不可信的东西。**

## 真实语料

仓库里**不放任何第三方数据**。用 `--corpus` 指向本地 JSONL：

```jsonl
{"user": "今天开会开到想吐", "assistant": "那就别开会了"}
```

- **LCCC**（清华）https://github.com/thu-coai/CDial-GPT · Apache-2.0
- **豆瓣多轮** https://github.com/MarkWuNLP/MultiTurnResponseSelection · 研究用途
- **NaturalConv** https://github.com/elliott-king/NaturalConv · 研究用途

## 实测规模

```
一年 · 24709 条事件 · 4.0 MB · 生成 1.3s · 重放 0.02s
```

比文档里估的"一年不到 200 MB"小一个数量级——按"只在工位采样"的真实密度，
一天约 60 条。**这该改进文档，而不是反过来调采样率去凑。**
