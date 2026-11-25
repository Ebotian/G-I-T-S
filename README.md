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

```bash
uv run --python 3.10 --with pytest python -m pytest tests -q     # 9 passed
```

## 文档

- [`docs/DESIGN.md`](docs/DESIGN.md) — 设计文档。定位与判据、events 真相模型、记忆/触发/装配/叙事/运动皮层/安全、成本、评测、生态位、运行环境、社交层、节律、多端、评审处置、网络与行动
- [`docs/schema.md`](docs/schema.md) — 表结构草案（**不是契约**）：三库分治、迁移纪律、保留与删除、时间戳三字段
- [`docs/impl.md`](docs/impl.md) — 实现方案：三条判据、性能预算、技术栈与拒绝清单、代码结构、首个里程碑
- [`docs/VERIFIED.md`](docs/VERIFIED.md) — 外部依赖事实与验证记录（带出处与日期）
- [`docs/research/sna.md`](docs/research/sna.md) — N=2..5 时仍成立的 SNA 方法
- [`docs/research/rhythm.md`](docs/research/rhythm.md) — 节律：作息是学出来的分布，不是配置的时段
