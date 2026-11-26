# G-I-T-S

**Ghost in the Shell** —— 一个陪伴体内核。设计先行，P0 骨架已可运行。

```
ghost    意识内核：唯一写者、唯一真相、唯一对外出口
shell    身体与外设：微信 / 工作机播放 /（可选）Mumble
dream    睡眠与巩固：批量作业调度
```

> 她有自己的内心生活，不是待命的接口。

## P0 现在能做什么

```bash
python -m gits.cli --db /tmp/g.db say "今天下午开会开到吐"
python -m gits.cli --db /tmp/g.db status
python -m gits.cli --db /tmp/g.db replay --restore
python -m gits.cli --db /tmp/g.db trace 1
```

验收标准是四条可执行的判据（`docs/impl.md` §7），全部有测试覆盖：

| # | 判据 |
|---|---|
| 1 | **提交后才确认** —— ACK 之后立刻 `os._exit(9)`，数据仍在 |
| 2 | **崩溃后重复提交只入库一次** —— `(source, ext_id)` 幂等键 |
| 3 | **空库离线重放 → 相同投影** —— 且缺模型结果时**硬失败**，不偷偷重算 |
| 4 | **一条回复可追到叙事版本与输入** —— `influence` + `model_calls` |

基建层（M1/M2，全部在合成数据上验证，不联网不调模型）：

| 模块 | 保证 |
|---|---|
| `context.py` | **A 段签名里没有时间** —— 缓存命中的守门人；测试扫描 A 段里不许出现时刻 |
| `rhythm.py` | 先问"有没有节律"（Lomb-Scargle，不均匀采样），再谈周期核密度；断线时 `s_user` 返回 `None` 而不是 0 |
| `gate.py` | `开口价值 = 未解释 × 可解释 × (1-积压) × 温度 × 0.85^n`，每个决策必带 `reason` |

```bash
uv run --python 3.10 --with pytest --with numpy --with statsmodels \
  python -m pytest tests -q          # 108 passed（+1 slow）
```

## 合成数据（`synth/`）

规模不必大，但**必须带真值**——统计类的机制没法靠读代码验证。

```bash
python -m synth all --days 30        # 五个场景 + 打分，输出 var/synth/report.json
python -m synth build --scenario drift --days 60 --out var/synth/drift60
```

五个场景各验证一个机制：`regular`（能学）、`night_owl`（双峰）、`drift`（会漂移）、
`chaotic`（**该承认自己不知道**）、`offline_gap`（断线=未观测，不是不在）。

数据走正规 `Writer` 写入，所以经过 `ext_id` 幂等 / `lag` / `ts_trust` 全套——
验证的是线上会跑的代码。细节见 [`synth/README.md`](synth/README.md)。

## 文档

- [`docs/DESIGN.md`](docs/DESIGN.md) — 设计文档。定位与判据、events 真相模型、记忆/触发/装配/叙事/运动皮层/安全、成本、评测、生态位、运行环境、社交层、节律、多端、评审处置、网络与行动
- [`docs/schema.md`](docs/schema.md) — 表结构草案（**不是契约**）：三库分治、迁移纪律、保留与删除、时间戳三字段
- [`docs/impl.md`](docs/impl.md) — 实现方案：三条判据、性能预算、技术栈与拒绝清单、代码结构、首个里程碑
- [`docs/VERIFIED.md`](docs/VERIFIED.md) — 外部依赖事实与验证记录（带出处与日期）
- [`docs/research/sna.md`](docs/research/sna.md) — N=2..5 时仍成立的 SNA 方法
- [`docs/research/rhythm.md`](docs/research/rhythm.md) — 节律：作息是学出来的分布，不是配置的时段
- [`docs/research/stats-libs.md`](docs/research/stats-libs.md) — 统计库用哪个、不用哪个、**在哪一层用**（ECS 实测 import 耗时与 RSS）
- [`docs/research/multi-node.md`](docs/research/multi-node.md) — 多端调度：频率 × 硬件 × 同步节律；依赖预算与 per-node 安装
- [`docs/research/sync.md`](docs/research/sync.md) — 分层同步：人格秒级、真相按计算需求、投影不传、大产物只传身份
- [`docs/architecture.md`](docs/architecture.md) — 代码结构：分层、依赖方向、为什么 time 在底部
- [`docs/seed-format.md`](docs/seed-format.md) — 初始化包格式：机器生成、人不看内容、只解析
- [`docs/research/funnel.md`](docs/research/funnel.md) — 记忆漏斗：容量是形状，复现是动力
- [`docs/research/peers.md`](docs/research/peers.md) — 三个实例：差异的物质基础（机制，不含内容）
- [`docs/research/loop.md`](docs/research/loop.md) — 从一条微信到她回话：链路、实测、三个还没做对的
- [`docs/research/weixin.md`](docs/research/weixin.md) — 微信 iLink Bot 通道：协议、四个只有真跑才暴露的坑
- [`docs/research/ecs.md`](docs/research/ecs.md) — ECS 上到底有什么：实测耗时/内存，以及还差哪六件事
- [`docs/research/retrieval.md`](docs/research/retrieval.md) — 检索：间接提问的词面召回是 0，embedding 不是可选优化
- [`docs/research/memory-data.md`](docs/research/memory-data.md) — 记忆的实验数据与一个否定结论：合成数据能测实现、不能测理论
- [`docs/research/replica.md`](docs/research/replica.md) — 可恢复的单向副本：游标语义、verify 在比什么、刻意没做的三件事
- [`docs/research/autonomy.md`](docs/research/autonomy.md) — 她不在你面前时用什么额度做什么：三等分、日清、形式自由、内容不可见
- [`docs/research/convergent.md`](docs/research/convergent.md) — 共同被独立实现的机制：八条不变式、各自破过的地方、怎么防止被改回去
- [`docs/research/amap.md`](docs/research/amap.md) — 外面的世界：读数是第三类东西（不是事件、不是记录）、位置是配置不是 IP 猜的
- [`docs/research/space.md`](docs/research/space.md) — 空间源：手机在 CGNAT 后面所以只能手机推、四个只有真装才暴露的坑、坐标换标签才出门
