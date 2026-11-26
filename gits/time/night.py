"""夜里：什么时候算"他睡了"，以及这时候该不该动人格。

为什么不用墙钟：墙钟是标签层，不是决策层。而且"几点算夜里"因人而异——
一个人可能 3 点睡，另一个人 23 点睡，写死时间就是在替我猜。

所以两个条件，缺一不可：

  **位置**  WeeklyKDE 在这一格的密度落在低位尾部（这个时段长期没人说话）
  **状态**  已经闲得比该时段的历史间隔中位数更久（今晚确实也空着）

只看位置，会碰上偶尔晚睡；只看状态，会把白天的长会当成夜里。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gits.core.db import DB
from gits.time.idle import Idle, estimate, gaps_of
from gits.time.rhythm import WeeklyKDE, bin_of

#: 密度低于全周的这个分位才算"安静时段"
QUIET_QUANTILE = 0.25


@dataclass(frozen=True)
class Night:
    ok: bool
    why: str
    idle: Idle | None = None


def check(db: DB, now_ms: int, *, actor: str = "her") -> Night:
    rows = db.q("SELECT ts FROM events WHERE kind='msg_in' ORDER BY ts DESC LIMIT 500")
    stamps = [int(r["ts"]) for r in rows]
    if len(stamps) < 40:
        return Night(False, f"只有 {len(stamps)} 条消息，看不出夜里是什么样")

    kde = WeeklyKDE().fit([(t, 1.0) for t in stamps], now_ms)
    n = kde.normalised()
    cur = float(n[bin_of(now_ms)])
    quiet = float(np.quantile(n, QUIET_QUANTILE))
    if cur > quiet:
        return Night(False, f"这一格密度 {cur:.4f} 高于安静线 {quiet:.4f}")

    # 状态：已经闲了多久 vs 这个时段平常的间隔
    gaps = gaps_of(stamps)
    elapsed = now_ms - max(stamps)
    typical = float(np.median(gaps)) if gaps.size else 0.0
    if elapsed <= typical:
        return Night(False, f"才闲了 {elapsed // 60_000} 分钟，"
                      f"这个时段平常 {typical / 60_000:.0f} 分钟")

    idle = estimate(db, now_ms, kde=kde)
    return Night(True, f"安静时段（{cur:.4f} ≤ {quiet:.4f}），已闲 {elapsed // 60_000} 分钟",
                 idle=idle)


def enough_time(night: Night, need_ms: int, *, margin: float = 1.5) -> bool:
    """活干得完吗。用 p10 而不是 p50——干到一半他回来了，比不干更糟。"""
    if not night.ok or night.idle is None:
        return False
    return night.idle.at_least(need_ms, margin=margin)
