"""她心里悬着的事：未解释度 / 可解释度从这里来。

`gate.py` 一直有两个空着的浮点数——`unexcluded`（未解释度）和 `resolvable`
（可解释度）——但**没有代码算它们**。这个模块把它填上，而且不新造结构：
两个量都从已有的实体图里导出来。

    未解释度  被反复提到，我们对它却几乎一无所知
              （有 mentions 边，却几乎没有 rel: 边）
    可解释度  这条线索还热不热。冷了的线索，问了也白问

**没有时钟。** 热不热用的是 `graph.effective`——带遗忘的强度，
和记忆衰减同一把尺子。所以「凌晨三点她不该冒话」不是一条规则，
而是那个时段 mentions 早就凉了，可解释度自己就低。

压力另算：距上次**主动**开口越久，门槛越低（`gate.threshold_for`）。
没有它的话主动行为会饿死——所有因子偏低时永远不开口，且不报错。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from gits.core.db import DB
from gits.speech.gate import GateInputs
from gits.memory.graph import effective

#: 知道这么多条之后就"没什么可问的了"
ENOUGH = 3.0
#: 少于这么多次提及的实体不进候选（一次提起是噪声）
MIN_MENTIONS = 2
#: 压力的时间常数缺省值（毫秒）。调用方可以传从节律学到的值。
TAU_HOURS = 36.0


@dataclass(frozen=True)
class Dangling:
    """一件悬着的事。"""

    entity: str
    unexcluded: float
    resolvable: float
    mentions: int
    known: int

    @property
    def value(self) -> float:
        return self.unexcluded * self.resolvable


def about(db: DB, eid: str, *, now: int) -> tuple[int, int, float]:
    """(提及次数, 已知条数, 强度)。强度带遗忘，所以"热"是衰减之后的。"""
    mentions = 0
    strength = 0.0
    for r in db.q("SELECT w, last_coact FROM edges WHERE dst=? AND kind='mentions'",
                  (eid,)):
        mentions += 1
        strength += effective(float(r["w"]), r["last_coact"], now)
    known = int(db.scalar(
        "SELECT count(*) FROM edges WHERE kind LIKE 'rel:%'"
        " AND (src=? OR dst=?)", (eid, eid)) or 0)
    return mentions, known, strength


def scan(db: DB, *, now: int, limit: int = 5) -> list[Dangling]:
    """所有悬着的事，按"值不值得问"排序。"""
    out: list[Dangling] = []
    seen: set[str] = set()
    for r in db.q("SELECT DISTINCT dst FROM edges WHERE kind='mentions'"):
        eid = str(r["dst"])
        if eid in seen:
            continue
        seen.add(eid)
        mentions, known, strength = about(db, eid, now=now)
        if mentions < MIN_MENTIONS:
            continue
        unexcluded = 1.0 - min(1.0, known / ENOUGH)
        # 强度映射到 (0,1)：提得越多、越近，越热。没有上限，用比值而非绝对值
        hot = strength / (strength + 1.0)
        if unexcluded <= 0 or hot <= 0:
            continue
        out.append(Dangling(entity=eid, unexcluded=unexcluded,
                            resolvable=hot, mentions=mentions, known=known))
    out.sort(key=lambda d: -d.value)
    return out[:limit]


def pressure(elapsed_ms: float, *, tau_hours: float = TAU_HOURS) -> float:
    """憋了多久 → 0..1。**确定性**，不是随机数。

    用指数饱和而不是线性：刚说完话的时候几乎为零，
    过了一个时间常数接近一半，再久就逼近 1 但永远不到。
    """
    if elapsed_ms <= 0:
        return 0.0
    return 1.0 - math.exp(-(elapsed_ms / 3_600_000) / max(1e-6, tau_hours))


def since_last_open(db: DB, *, now: int) -> int | None:
    """距上次**主动**开口多久（毫秒）。没有主动开口过就返回 None。

    「主动」= outbox 里没有 ref_event_id 的行：不是回应某条消息，而是自己起的话头。
    peer 通道不算——那是实例之间的事。
    """
    row = db.one("SELECT max(ts) AS t FROM outbox"
                 " WHERE ref_event_id IS NULL AND channel != 'peer'")
    if row is None or row["t"] is None:
        return None
    return max(0, now - int(row["t"]))


def _seed_text(db: DB, *, now: int) -> str:
    """拿什么当"此刻的上下文"。最近一条收到的消息——那是共鸣的源头。"""
    row = db.one("SELECT payload FROM events WHERE kind='msg_in'"
                 " ORDER BY id DESC LIMIT 1")
    return (row["payload"] or b"").decode("utf-8", "replace") if row else ""


def inputs(db: DB, *, now: int, phase_allows: bool = True,
           channel_online: bool = True, backlog_pressure: float = 0.0,
           reciprocity: float | None = None, consecutive: int = 0,
           tau_hours: float = TAU_HOURS,
           actor: str = "her") -> tuple[GateInputs, float, Dangling | None]:
    """把图里的东西折成 GateInputs + 压力 + 那件悬着的事。"""
    best = None
    cands = scan(db, now=now, limit=1)
    if cands:
        best = cands[0]
    elapsed = since_last_open(db, now=now)
    # 从没主动开口过：按"已经憋了很久"算，否则第一次永远发不出去
    p = 1.0 if elapsed is None else pressure(elapsed, tau_hours=tau_hours)

    # 共鸣：他刚说的话和哪段旧记忆有多像。**已经有了**——recall 本来就按刺激召回，
    # 取最高相似度即可，不需要向量、不需要额外调用。
    from gits.memory.recall import recall
    from gits.social import state as slow

    hits = recall(db, _seed_text(db, now=now), actor=actor, now_ms=now, k=3)
    resonance = max((h.sim for h in hits), default=0.0)

    return (GateInputs(
        phase_allows=phase_allows, channel_online=channel_online,
        unexcluded=best.unexcluded if best else 0.0,
        resolvable=best.resolvable if best else 0.0,
        backlog_pressure=backlog_pressure, reciprocity=reciprocity,
        consecutive=consecutive, resonance=resonance,
        confidence=slow.read(db, actor, "confidence", now=now)), p, best)
