"""还要多久他才会说话——用已有的时序工具算，不另起一套。

三个来源，各自解决一个别的东西解决不了的问题：

  **经验间隔**  直接量。不需要模型，不需要假设，样本够就最准。
  **WeeklyKDE**  现在这一格是不是比平常热闹。经验间隔不分时段，KDE 补这一刀。
  **BOCPD**      最近有没有换生活节奏。换了，历史间隔就不该被当真。

刻意**不产出"某个具体时刻"**：那是墙钟在做决定。产出的是**分布**——
她要知道的是"大概还能闲多久"，而不是"21:30 他会来"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from gits.core.db import DB
from gits.time.rhythm import WeeklyKDE, bin_of, bocpd

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR

#: 少于这么多条间隔样本，就不该假装知道（宁可说"不知道"）
MIN_GAPS = 20


@dataclass(frozen=True)
class Idle:
    """预计空闲。三个分位，加一个她自己知道的可信度。"""

    p10_ms: int
    p50_ms: int
    p90_ms: int
    confidence: float          # 0..1，样本量 × 节律稳定度
    basis: str                 # 给人看的：凭什么这么估

    def at_least(self, need_ms: int, *, margin: float = 1.5) -> bool:
        """敢不敢开始一件要 need_ms 的活。

        用 p10 而不是 p50——预测本身有误差，整理到一半他回来了，
        比不整理更糟（窗口已经换过一次，记忆也断了一次）。
        """
        return self.p10_ms >= need_ms * margin


def gaps_of(timestamps: Sequence[int]) -> np.ndarray:
    ts = sorted(int(t) for t in timestamps)
    if len(ts) < 2:
        return np.zeros(0)
    d = np.diff(np.array(ts, dtype=np.int64))
    return d[d > 0]


def _density_factor(kde: WeeklyKDE, now_ms: int) -> float:
    """现在这一格比一周里平常热闹还是冷清。

    经验间隔是把所有时段混在一起算的，所以它不知道"凌晨三点没人说话"
    和"晚上八点没人说话"是两种事。这个因子补那一刀。
    """
    n = kde.normalised()
    cur = float(n[bin_of(now_ms)])
    med = float(np.median(n))
    if cur <= 0 or med <= 0:
        return 1.0
    # 比平常冷清 → 空闲可能更长；热闹 → 更短。夹在 [0.5, 3]，别让因子反过来主导。
    return float(min(3.0, max(0.5, med / cur)))


def estimate(
    db: DB,
    now_ms: int,
    *,
    limit: int = 500,
    kde: WeeklyKDE | None = None,
    recent_change: bool | None = None,
) -> Idle:
    """他大概还会闲多久。**没有足够样本就诚实地说不知道。**"""
    rows = db.q(
        "SELECT ts FROM events WHERE kind='msg_in' ORDER BY ts DESC LIMIT ?",
        (limit,))
    stamps = [int(r["ts"]) for r in rows]
    gaps = gaps_of(stamps)

    if len(gaps) < MIN_GAPS:
        return Idle(p10_ms=0, p50_ms=0, p90_ms=0, confidence=0.0,
                    basis=f"只有 {len(gaps)} 条间隔样本，不够")

    # 已经闲了多久（从上一条消息算起）
    elapsed = max(0, now_ms - max(stamps)) if stamps else 0
    # 条件存活：只留下"比现在已经闲得更久"的那些间隔
    longer = gaps[gaps > elapsed]
    beyond_history = longer.size < 5
    if beyond_history:
        # 他已经比 95% 的历史间隔都久——历史帮不上忙了，只能给一个"很久"
        base = np.array([float(np.percentile(gaps, 90)) * 2])
        conf = 0.15
    else:
        base = longer.astype(float) - elapsed
        conf = min(1.0, longer.size / 50.0)

    # 节律修正
    if kde is None:
        kde = WeeklyKDE().fit(
            [(t, 1.0) for t in stamps], now_ms)
    factor = _density_factor(kde, now_ms)
    if beyond_history:
        # 已经超过历史最长间隔了，再说"这一格平时很热闹所以他会快点回来"
        # 就是自相矛盾——那个判断本身就是"比平常久"。
        factor = max(1.0, factor)
    base = base * factor

    # 换节奏了，历史间隔就不该被当真：只降可信度，不改数字（改了就是编）
    if recent_change is None:
        recent_change = _changed_recently(stamps, now_ms)
    if recent_change:
        conf *= 0.5

    p10, p50, p90 = (int(np.percentile(base, q)) for q in (10, 50, 90))
    bits = [f"{len(gaps)} 条间隔", f"节律因子 {factor:.2f}"]
    if recent_change:
        bits.append("最近换过节奏")
    return Idle(p10_ms=max(0, p10), p50_ms=max(0, p50), p90_ms=max(0, p90),
                confidence=round(conf, 2), basis="，".join(bits))


def _changed_recently(stamps: Sequence[int], now_ms: int, *,
                      window_days: int = 28) -> bool:
    """最近一个月内的逐日消息量，有没有变点。

    用 BOCPD 而不是"最近七天的均值比上个月低"——后者对一天两天的
    波动很敏感，而这个判断的代价是**降低对她自己空闲预测的信任**。
    """
    if not stamps:
        return False
    days = window_days
    start = now_ms - days * DAY
    buckets = np.zeros(days)
    for t in stamps:
        if t < start:
            continue
        i = int((t - start) // DAY)
        if 0 <= i < days:
            buckets[i] += 1
    if buckets.sum() < MIN_GAPS:
        return False
    run = bocpd(buckets)
    # run 长度：离上一个变点有多远。最后一个点如果很短，说明刚变过。
    return bool(len(run) and run[-1] < 7)

