"""时序：**所有依赖时间的决策都从这里拿**，不要在别处再写一套。

这个包存在的理由不是好看。同一件事以前散在三处：

    rhythm.py   周期、周内密度、变点           ← 节律
    presence.py 他在不在的概率（泊松 GLM）      ← 在场
    idle.py     还会闲多久（经验间隔 + 生存）   ← 空闲
    night.py    现在算不算夜里（用上面两个）    ← 夜里

后果是**同一个问题会在不同模块里被回答两次，答案还不一样**。

对外只暴露几件事：

    density_at(db, t)     这个时刻她预期多热闹（0..N）
    is_quiet(db, t)       这个时刻是不是长期没人说话
    expected_idle(db, t)  还会闲多久（分布，不是时刻）
    presence_rate(db, t)  他在的概率（GLM，刻度是每小时的率）
    phase_of(t)           粗粒度时段标签（只做参照层，不做决策）

**没有一个函数返回"时刻"。**都返回分布、概率或分位数——
墙钟只做参照层，不参与行为决策（DESIGN §14.4）。
"""

from __future__ import annotations

from gits.core.db import DB
from gits.time.idle import Idle, estimate
from gits.time.night import Night, check as night_check
from gits.time.rhythm import (WeeklyKDE, bin_of, bocpd, periodicity_report,
                              s_user)

__all__ = [
    "WeeklyKDE", "bin_of", "bocpd", "periodicity_report", "s_user",
    "Idle", "estimate", "Night", "night_check",
    "density_at", "is_quiet", "expected_idle", "presence_rate",
]


def density_at(db: DB, t: int, *, lookback: int = 500) -> float:
    """这个时刻她预期多热闹。周内小时格的核密度，带遗忘。"""
    rows = db.q("SELECT ts FROM events WHERE kind='msg_in'"
                " ORDER BY ts DESC LIMIT ?", (lookback,))
    stamps = [int(r["ts"]) for r in rows]
    if not stamps:
        return 0.0
    return WeeklyKDE().fit([(x, 1.0) for x in stamps], t).density(t)


def is_quiet(db: DB, t: int) -> bool:
    """这个时刻是不是长期没人说话。**这是"凌晨不该冒话"的机制，不是规则。**"""
    return night_check(db, t).ok


def expected_idle(db: DB, t: int) -> Idle:
    """还会闲多久。样本不够时 confidence=0——诚实地说不知道。"""
    return estimate(db, t)


def presence_rate(db: DB, t: int) -> float | None:
    """他在的概率（每小时率）。没有模型或没数据就返回 None，不要折成 0。"""
    row = db.one("SELECT params FROM presence_model ORDER BY id DESC LIMIT 1")
    if row is None:
        return None
    from gits.time.presence import PoissonHarmonic

    model = PoissonHarmonic.from_json(str(row["params"]))
    return model.rate(t)
