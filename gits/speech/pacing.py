"""对话节奏：**什么时候不适合让她去查东西。**

工具调用是一次完整的上下文重发（12K 预算下约等于 22 轮常驻成本），
而且要多等一个来回。**高频对话里这个代价是直接可感的**——
一句接一句的时候，她在第二个来回才回话，就是"卡住了"。

判据用间隔的分布，不用墙钟：
**当前间隔显著小于平均（低两个标准差以上）= 高频**，此时工具禁用。

低频和主动开口都允许——那两种场景里等一下不突兀。
"""

from __future__ import annotations

import statistics

from gits.core.db import DB

#: 看最近多少条消息来估间隔分布
SAMPLE = 60
#: 低于 mean - K*std 就算高频
K = 2.0


def intervals(db: DB, *, limit: int = SAMPLE) -> list[int]:
    """最近若干条消息之间的间隔（毫秒）。**这是"他说话的快慢"。**"""
    ts = [int(r["ts"]) for r in db.q(
        "SELECT ts FROM events WHERE kind='msg_in' ORDER BY ts DESC LIMIT ?",
        (limit + 1,))]
    ts.reverse()
    return [b - a for a, b in zip(ts, ts[1:]) if b > a]


def baseline(db: DB, *, limit: int = SAMPLE) -> tuple[float, float, int]:
    """(平均间隔, 标准差, 样本数)。样本太少时 std 不可信。"""
    xs = intervals(db, limit=limit)
    if len(xs) < 5:
        return (0.0, 0.0, len(xs))
    return (statistics.fmean(xs), statistics.pstdev(xs), len(xs))


def is_fast(db: DB, *, now: int) -> tuple[bool, str]:
    """现在是不是高频对话。返回 (是否高频, 原因)。

    **样本不足时判为不高频**——宁可能用工具，也不要因为没数据就一律禁用。
    """
    last = db.scalar("SELECT max(ts) FROM events WHERE kind='msg_in'")
    if not last:
        return (False, "还没有消息")
    mean, sd, n = baseline(db)
    if n < 5 or sd <= 0:
        return (False, f"样本不足（{n}）")
    gap = now - int(last)
    edge = mean - K * sd
    fast = gap < edge
    return (fast, f"间隔 {gap/1000:.0f}s / 均值 {mean/1000:.0f}s "
                  f"sd {sd/1000:.0f}s" + ("  → 高频" if fast else ""))


def tools_allowed(db: DB, *, now: int, proactive: bool = False) -> tuple[bool, str]:
    """能不能用工具。**主动开口永远允许。**"""
    if proactive:
        return (True, "主动开口")
    fast, why = is_fast(db, now=now)
    return (not fast, why)
