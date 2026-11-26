"""驱动：**正反馈，上界 + 限速 + 限加速度**。

不是恒温器。恒温器的做法是「说多了就压一压」，那会把关系拉回一个恒定速率——
变成温控，不是人。

这里要的是动量：

    最近说得密  →  drive 高  →  同样一条证据也更想开口  →  说得更密

但三样东西把它按住，缺一样就会跑飞：

    **上界**    drive ∈ [0,1]，0.5 是中性
    **限速**    每小时最多挪 0.15
    **限加速度** 速度每小时最多变 0.10

为什么加速度也要限：只限速率的话，速度可以一步跳到最大，
曲线在起点最陡、后面平掉——像「突然想要，然后慢慢泄气」。

低方向同样成立（一直很少说话 → drive 低 → 更难开口）。
那看起来像"越来越远"，但正确的做法不是在这里加负反馈，
而是让**压力**（unresolved.pressure）去对抗它：憋得越久门槛越低。
两个机制方向相反，这才是人同时有的两件事：惯性，和想念。
"""

from __future__ import annotations

from gits.core.db import DB, Writer
from gits.social import state as slow
from gits.time.regulation import slew

#: cadence 相对基线偏离多少倍 → 目标 drive 偏离中性多少
GAIN = 0.8
#: 每小时最多挪多少
MAX_RATE = 0.15
#: 速度每小时最多变多少（二阶限幅）
MAX_ACC = 0.10
#: drive 对开口价值的影响幅度（0.4 → 最多 ×1.4 / ×0.6）
EFFECT = 0.4
NEUTRAL = 0.5


def target_of(cadence: float, baseline: float) -> float:
    """目标 drive。cadence 高于基线 → 大于中性。**这是正反馈的那一半。**"""
    if baseline <= 0:
        return NEUTRAL
    ratio = cadence / baseline
    return max(0.0, min(1.0, NEUTRAL + GAIN * (ratio - 1.0) * NEUTRAL))


def _load(db: DB, actor: str) -> tuple[float, float, int]:
    row = db.one("SELECT value, rate, ts FROM drive WHERE actor=?", (actor,))
    if row is None:
        return NEUTRAL, 0.0, 0
    return float(row["value"]), float(row["rate"]), int(row["ts"])


def read(db: DB, actor: str, *, now: int) -> float:
    """当前值。**只读**：从存下来的点往前推到 now，不落库。"""
    value, rate, ts = _load(db, actor)
    if ts <= 0:
        return NEUTRAL
    cadence = slow.read(db, actor, "cadence", now=now)
    base = float(slow.CFG["cadence"]["baseline"])
    dt_h = max(0.0, (now - ts) / 3_600_000)
    new, _r = slew(value, rate, target_of(cadence, base), dt_h,
                   max_rate=MAX_RATE, max_acc=MAX_ACC)
    return new


def update(db: DB, actor: str, *, now: int) -> float:
    """推进一格并落库。由 ghost 每拍调用；gate 只读不写。"""
    value, rate, ts = _load(db, actor)
    cadence = slow.read(db, actor, "cadence", now=now)
    base = float(slow.CFG["cadence"]["baseline"])
    dt_h = 0.0 if ts <= 0 else max(0.0, (now - ts) / 3_600_000)
    new, new_rate = slew(value, rate, target_of(cadence, base), dt_h,
                         max_rate=MAX_RATE, max_acc=MAX_ACC)
    with Writer(db).txn() as c:
        c.execute(
            "INSERT INTO drive(actor, value, rate, ts) VALUES(?,?,?,?)"
            " ON CONFLICT(actor) DO UPDATE SET value=?, rate=?, ts=?",
            (actor, new, new_rate, now, new, new_rate, now))
    return new


def factor(value: float) -> float:
    """gate 用的乘子：[0.6, 1.4]。中性 0.5 时正好 1.0。"""
    v = max(0.0, min(1.0, float(value)))
    return 1.0 + EFFECT * (v - NEUTRAL) * 2.0
