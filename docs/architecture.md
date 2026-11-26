# 代码结构

> 43 个模块平铺在一个目录里的时候，找东西靠 grep、依赖方向靠猜。
> 分了包之后，**每一层的依赖方向应该是单向的**——
> 如果发现 A 包 import 了 B 包，而 B 也 import 了 A，那不是巧合，是设计漏了。

## 目录

```
gits/
  core/     真相与投影
    db.py        单写者、迁移、WAL
    events.py    日志集、稳定身份、导出导入
    project.py   恢复重放（不调模型；缺结果就抛 MissingModelResult）
    scope.py     可见域——**一个门**决定谁看得见什么
    seedfile.py  初始化包（机器生成、人不看内容）
    replica.py   单向副本，拉取式、断点续传
    sync.py      分层同步流
    wipe.py      清空内容、保留机制

  time/     时序——**所有依赖时间的决策都从这里拿**
    rhythm.py    周期、周内密度、变点（Lomb-Scargle / WeeklyKDE / BOCPD）
    presence.py  他在不在（泊松 GLM）
    idle.py      还会闲多久（经验间隔 + 生存分析）
    night.py     现在算不算夜里（用上面两个）
    decay.py     遗忘模型对比（实验用）

  memory/   记、忘、压、找
    fsrs.py      遗忘曲线
    extract.py   消息 → 结构化事实
    graph.py     带遗忘的共现计数（统计沉淀）
    semantic.py  实体别名 + 共现推断（**没有向量**）
    funnel.py    容量 + 复现决定的压缩
    search.py    字符二元组 + BM25
    recall.py    生产检索路径
    persona.py   夜里从自我域里选 A 段
    compact.py   关窗 = 写成一条记忆

  social/   个体之间
    actors.py    身份（数据，不是配置）
    peer.py      上限 / 回合 / 投递 / 观测
    affect.py    情绪：24 格分布 + 连续流动
    attention.py 注意力：锚点从数据里估

  speech/   说话
    context.py   A/B/C 三段装配（**形状就是缓存策略**）
    ctxwin.py    上下文窗口：什么时候换一扇新的
    gate.py      开口决策：价值 + 压力，**没有一条规则读时钟**
    unresolved.py 悬着的事：未解释度 / 可解释度
    respond.py   一轮回复：装配 → 调模型 → 落 outbox
    llm.py       模型调用（thinking 必须显式关掉）
    facts.py     固定事实（生日这类**记录**，不是记忆）

  io/       出入口
    shell.py     通道协议
    delivery.py  outbox 租约状态机
    quota.py     额度账户
    channels/    具体协议（微信 iLink）

  cli.py        命令行入口
  ghost.py      常驻进程（心跳 / 回复 / 快照 / 整理）
  sched.py      跨节点任务放置（三硬约束）
  containment.py 隔离自检（不拦，只说）
```

## 依赖方向

```
io ──▶ speech ──▶ memory ──▶ core
        │           ▲
        └──▶ social ┘
                 │
                 ▼
               time   （谁都能用，它不依赖任何人）
```

**`time` 在底部**：它是纯算法（核密度、GLM、生存分析、变点检测），
不读库、不认识节点、不知道谁是谁。往上每一层都可以用它。

这一条容易破：某个模块想用“现在几点”，就顺手写 `datetime.now()`。
那等于在 `time` 之外又长了一个时序实现——而它与 `time` 给出的答案会不一致。
所以规矩是：

> **要问时间，问 `gits.time`。不要在别处读钟。**

`gits/time/__init__.py` 只暴露五个函数：

| 函数 | 回答什么 |
|---|---|
| `density_at(db, t)` | 这个时刻她预期多热闹 |
| `is_quiet(db, t)` | 这个时刻是不是长期没人说话 |
| `expected_idle(db, t)` | 还会闲多久（**分布**，不是时刻） |
| `presence_rate(db, t)` | 他在的概率（每小时率，没有就 `None`） |
| `bin_of(t)` | 周内小时格（坐标，不是时刻） |

**没有一个返回“时刻”。** 都返回分布、概率或分位数——
墙钟只做参照层（`recv_ts` 排序、`bucket()` 折叠），不参与行为决策。

## 三条不该破的规矩

**① 依赖单向。** 发现互相 import，不是巧合，是某个概念放错了包。

**② 只有 `core` 写库。** 别的包拿到 `DB` 是为了读和通过 `Writer` 提交一个事务，
不是各自开连接（单写者是有意的）。

**③ `time` 不 import 任何业务模块。** 一旦它需要知道“什么是实体”，它就不再是纯算法了。

## 还没归位的

- `ghost.py` / `cli.py` / `sched.py` 留在顶层：它们是**入口**，不是库。
- `sched.py` 其实更像 `runtime/`（跨节点放置），但只有一个文件，先不动。
- 包门面（`__init__.py` 的 re-export）目前只给 `time` 做了完整的一套，
  其余包只有文档字符串。用得到再加，不预先铺。
