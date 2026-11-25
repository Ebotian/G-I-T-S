"""分层同步：不同东西有不同频率，因为它们的消费方不同。

DESIGN §15.8 原来只说"同步日志集"，那是**一层**。实际上：

  T0 人格文件   每次开口都要读     → 变了就传（它只有几 KB）
  T1 真相事件   按计算需求         → 工作机 30min、云上持续
  T2 派生投影   **不传**，各自重放 → 传了反而会打架
  T3 大产物     不传               → 只传"我是用什么产的"这行元数据

关键在于**计算依赖各端不同，产物因此不同**。这不是问题，前提是产物带着
producer 标记：拿 bge-m3@v1 产出的向量和 v2 的混在一个索引里，检索会静默变差。
"""

from __future__ import annotations

from dataclasses import dataclass

MIN = 60_000
HOUR = 3_600_000

#: 层的语义。数字是"消费方的容忍度"，不是拍脑袋的刷新周期。
TIERS = {
    0: "人格（读了会影响她是谁）",
    1: "真相（唯一可信的历史）",
    2: "投影（可以从真相重算）",
    3: "大产物（机器相关，不可移植）",
}


@dataclass(frozen=True)
class Stream:
    name: str
    tier: int
    direction: str            # 'both' | 'up' | 'down' | 'none'
    consumers: tuple[str, ...]
    staleness_max_ms: int     # 消费方最多能忍多久
    why: str = ""

    @property
    def syncable(self) -> bool:
        return self.direction != "none"


#: 同步表。顺序即优先级：人格最先，大产物最后（且不传）。
STREAMS: tuple[Stream, ...] = (
    Stream("self_narrative", 0, "both", ("context", "persona"), 5 * MIN,
           "每次开口都在 A 段里，但它只有几 KB——变了就传"),
    Stream("attitudes", 0, "both", ("gate", "social"), 30 * MIN,
           "三元平衡要读；变化慢"),
    Stream("rituals", 0, "both", ("gate", "social"), HOUR,
           "EE 结算用它；一天结一次"),
    Stream("presence_model", 0, "both", ("gate", "sched"), HOUR,
           "只是几行系数，重算要 2 秒 import statsmodels——传比算便宜"),
    Stream("events", 1, "both", ("memory", "rhythm"), 30 * MIN,
           "各自推自己产生的（麦克风/空闲只有工作机有），回环按 ext_id 合并"),
    Stream("model_calls", 1, "both", ("restore", "cost"), 30 * MIN,
           "恢复重放的必要输入"),
    Stream("observability", 1, "both", ("rhythm",), HOUR,
           "「她当时看不看得见」——缺了它会把断线学成不在"),
    Stream("redactions", 1, "both", ("privacy",), 10 * MIN,
           "删除必须尽快传导；比新增更急"),
    Stream("nodes", 2, "none", (), 0, "从日志集重放，不传"),
    Stream("edges", 2, "none", (), 0, "从日志集重放，不传"),
    Stream("embedding", 3, "none", ("memory",), HOUR,
           "不可移植：不同模型/版本产出的向量不可混用；只传 producer 元数据"),
    Stream("acoustic", 3, "none", ("memory",), 0, "原始音频永不上云（§15.6）"),
)

BY_NAME = {s.name: s for s in STREAMS}


def producer_of(node_id: str, model: str, version: str) -> str:
    """产物的身份。**换了模型就必须换 producer**，否则旧向量会混进新索引。"""
    return f"{node_id}/{model}@{version}"


def compatible(a: str, b: str) -> bool:
    """两个产物能不能混用：producer 必须完全一致。"""
    return a == b


@dataclass(frozen=True)
class SyncPlan:
    stream: str
    tier: int
    action: str        # 'push' | 'pull' | 'skip'
    reason: str


def plan(node_id: str, *, sync_period_ms: int, freshness_ms: int,
         pending_up: dict[str, int]) -> list[SyncPlan]:
    """这一次该传什么。

    只做两件事：**按容忍度排序**、**把做不到的如实标出来**。
    做不到不等于失败——它意味着这个流必须由本地生产。
    """
    out: list[SyncPlan] = []
    for s in sorted(STREAMS, key=lambda x: (x.tier, x.staleness_max_ms)):
        if not s.syncable:
            out.append(SyncPlan(s.name, s.tier, "skip", "不传（重放或不可移植）"))
            continue
        if s.staleness_max_ms < sync_period_ms:
            out.append(SyncPlan(
                s.name, s.tier, "skip",
                f"做不到：容忍 {s.staleness_max_ms // MIN}min，"
                f"本节点 {sync_period_ms // MIN}min 才同步一次 → 必须本地生产",
            ))
            continue
        if s.direction in ("both", "up") and pending_up.get(s.name):
            out.append(SyncPlan(s.name, s.tier, "push",
                                f"{pending_up[s.name]} 条待上传"))
        elif s.direction in ("both", "down") and freshness_ms >= min(
                s.staleness_max_ms, sync_period_ms):
            out.append(SyncPlan(s.name, s.tier, "pull",
                                f"已 {freshness_ms // MIN}min 没取"))
        else:
            out.append(SyncPlan(s.name, s.tier, "skip", "还新鲜"))
    return out


def echo_merge(local_pending: list[dict], pulled: list[dict]) -> list[dict]:
    """回环合并：自己推上去的事件会被自己拉回来，必须按 (source, ext_id) 丢掉。

    这一步不做，本地副本会把自己的事件算两遍（一次 pending、一次 canonical），
    于是"她说过的话"在本地比在云上多一倍。
    """
    seen = {(e["source"], e["ext_id"]) for e in pulled}
    return [e for e in local_pending if (e["source"], e["ext_id"]) not in seen]
