"""主动开口：压力按"闲的时长"积累，惩罚按"该回却没回"加权。

这条路径以前是断的：gate.evaluate() 有决策逻辑，但**没有任何调用方**；
respond() 支持 proactive 参数，但没人传。决策机制齐了，没人按下按钮。

## 压力不是墙钟，是"闲的时间"

同样是隔了 3 小时，他一直在开会和他一直闲着，是两件事。
所以压力 = ∫ 他可能闲着的概率 dt，而不是 now - last。

    他忙的那一小时    几乎不加压
    他闲的一小时      加满一格

这就是"预测闲的时候增长快"。而且它顺带解释了为什么不需要频率闸：
闲得久，压力自然满；连着说，压力自然清零。

## 惩罚不是"没回就该罚"，是"该回却没回才罚"

她在**他大概率在线**的时候说话，却没收到回复——这是意外，罚得重。
她在**他大概率在开会**的时候说话，没回复——这本来就在预期里，罚得轻。

这是"沉默是含糊的"在主动开口上的版本：沉默的信息量取决于当时他多可能说话。

## 只在最终决策时采样

整条链上其它地方都是分布：在场率、闲的概率、价值。
只有"说还是不说"这一步是一次抽样——因为决定本身就是一次选择。
种子按（actor，分钟）固定，所以同一分钟内问两次得到同一个答案。
"""

from __future__ import annotations

import hashlib
from typing import Any

from gits.core.db import DB

#: 攒满压力需要多少"闲的小时"。不是墙钟小时——是加权后的。
IDLE_HOURS_FULL = 6.0
#: 一次"该回却没回"最多扣掉多少（乘以当时的在线概率）。
UNREPLIED_WEIGHT = 0.45
#: 他闲够这么久还没回，就算整笔欠下。
#:
#: **这个数受在场估计的分辨率限制。** 粗估用的是"这个小时出现过多少消息"，
#: 而"他回了没"看的也是消息——两者同源，所以"在线却不回"能撑的时间很短，
#: 通常不超过一小时。拟合出逐小时的率之后这个数可以再调大。
REPLY_HORIZON_HOURS = 1.0
#: 多久没回算"没回"
REPLY_WINDOW_MS = 6 * 3_600_000


def spoken(db: DB, *, actor: str = "her") -> list[int]:
    """主动开口的时间戳，最近的在前。"""
    return [int(r["ts"]) for r in db.q(
        "SELECT ts FROM events WHERE kind='initiative' AND scope=?"
        " ORDER BY ts DESC LIMIT 40", (f"self:{actor}",))]


def online(db: DB, *, ts: int, snap: Any = None) -> float:
    """那一刻他大概在线的概率。

    用 `time/activity`：5 分钟细桶收缩到小时先验——空的地方成片，
    它们退回小时估计，不发明精度；有数据的地方才有自己的分辨率。

    `presence_rate`（夜里的泊松 GLM）是另一条路，但它现在在生产上从没成功过
    （`presence_model` 缺 `params` 列，异常被吞），所以不再假装先问它。
    """
    from gits.time import activity

    return activity.at(db, ts, now=ts, snap=snap)


def opportunity(db: DB, *, start: int, end: int, snap: Any = None) -> float:
    """[start, end] 之间他大概闲了几个小时。**压力的原材料，也是欠账的原材料。**

    按小时切、按当时的在线概率加权。两个量用的是同一个积分——
    区别只在窗口：压力看「距上次开口到现在」，欠账看「说完之后那一段」。
    """
    if end <= start:
        return 0.0
    hour = 3_600_000
    acc = 0.0
    t = start
    while t < end:
        span = min(hour, end - t) / float(hour)
        acc += online(db, ts=t, snap=snap) * span
        t += hour
    return acc


def pressure(db: DB, *, now: int, actor: str = "her",
             snap: Any = None) -> float:
    """攒了多少"闲的时间"。0 = 刚说过，1 = 攒满。

    **积的是他可能闲着的时长，不是墙钟。** 一小时一小时地累加，
    每小时按当时的在线概率加权。
    """
    last = spoken(db, actor=actor)
    if not last:
        # 从没主动说过话 —— 没有在攒的东西，也没有欠的东西。
        # 不能退到"最近 6 个墙钟小时"：那会让一个从没开口的实例永远攒不满。
        return 1.0
    start = last[0]
    if now <= start:
        return 0.0
    acc = opportunity(db, start=start, end=now, snap=snap)
    return max(0.0, min(1.0, acc / IDLE_HOURS_FULL))


def unanswered(db: DB, *, now: int, actor: str = "her",
               window: int = REPLY_WINDOW_MS,
               snap: Any = None) -> tuple[float, float]:
    """没收到回复的累积惩罚。返回 (惩罚系数, 加权次数)。

    **允许重发**，但每欠一次就乘一个 (1 - UNREPLIED_WEIGHT * 在线概率)。
    在他忙的时候没回，几乎不扣；在他闲的时候没回，扣得重。
    """
    factor = 1.0
    weight = 0.0
    for ts in spoken(db, actor=actor):
        if ts > now:
            continue
        replied = db.scalar(
            "SELECT count(*) FROM events WHERE kind='msg_in' AND ts>? AND ts<=?",
            (ts, ts + window))
        if replied:
            continue
        # **欠账也是那个积分**：说完之后他闲了几个小时。
        # 归一化到 REPLY_HORIZON——他闲够两个钟头还没回，就算整笔欠下。
        idle = opportunity(db, start=ts, end=min(now, ts + window),
                           snap=snap)
        debt = min(1.0, idle / REPLY_HORIZON_HOURS)
        factor *= max(0.0, 1.0 - UNREPLIED_WEIGHT * debt)
        weight += debt
    return (factor, weight)


def _seed(actor: str, now: int) -> int:
    h = hashlib.sha256(f"{actor}:{now // 60_000}".encode()).hexdigest()
    return int(h[:8], 16)


def sample_value(mean: float, *, actor: str, now: int, n: float = 8.0) -> float:
    """把"价值大致是这个数"变成一个具体的数。**只在决策时用一次。**

    用 Beta(mean*k, (1-mean)*k) 抽样：k 是不确定度，观测少就抖得厉害。
    种子按（actor，分钟）固定——同一分钟内问两次，答案一样（可复现）。
    """
    import random

    mean = max(0.001, min(0.999, float(mean)))
    k = max(1.0, float(n))
    a, b = mean * k, (1.0 - mean) * k
    rnd = random.Random(_seed(actor, now))
    return rnd.betavariate(a, b)


def assemble(db: DB, *, now: int, actor: str, cand: Any,
             channel_online: bool = True):
    """把库里的东西装成 GateInputs。**gate 保持纯函数，不碰 DB。**"""
    from gits.social import drive
    from gits.social import state as slow
    from gits.speech.gate import GateInputs
    from gits.speech.respond import pending

    try:
        conf = slow.read(db, actor, "confidence", now=now)
    except Exception:
        conf = None
    try:
        mom = drive.read(db, actor, now=now)
    except Exception:
        mom = None
    backlog = min(1.0, len(pending(db, actor=actor, limit=5)) / 3.0)
    consec = len([t for t in spoken(db, actor=actor) if now - t < 6 * 3_600_000])
    penalty, _w = unanswered(db, now=now, actor=actor)
    return GateInputs(
        channel_online=channel_online,
        unexcluded=float(cand.unexcluded), resolvable=float(cand.resolvable),
        backlog_pressure=backlog, consecutive=consec,
        confidence=conf, drive=mom, presence=online(db, ts=now),
        unanswered=penalty)


def check(db: DB, *, now: int, actor: str = "her",
          channel_online: bool = True) -> dict[str, Any]:
    """该不该主动开口。**返回理由，不只是布尔。**"""
    from gits.speech import gate
    from gits.speech.unresolved import sample

    p = pressure(db, now=now, actor=actor)
    cand = sample(db, now=now, actor=actor)
    if cand is None:
        return {"allow": False, "why": "没有悬着的事", "pressure": round(p, 3)}

    inputs = assemble(db, now=now, actor=actor, cand=cand,
                      channel_online=channel_online)
    base = gate.evaluate(inputs, pressure=p)
    # **采样只在这一步**：之前全是分布，这里是唯一一次取值。
    drawn = sample_value(base.value, actor=actor, now=now)
    ok = bool(base.allow) and drawn >= gate.threshold_for(p)
    penalty, weight = unanswered(db, now=now, actor=actor)
    return {"allow": ok,
            "why": base.reason + ("" if not weight else
                                  f"；欠回复 {weight:.1f} 加权次，惩罚 {penalty:.2f}"),
            "value": round(drawn, 3), "expected": round(float(base.value), 3),
            "pressure": round(p, 3), "penalty": round(penalty, 3),
            "presence": round(inputs.presence or 0.0, 2),
            "candidate": getattr(cand, "entity", None)}


def record(db: DB, *, now: int, reason: str, candidate: str | None,
           actor: str = "her") -> int | None:
    """落一条 initiative 事件。**记的是「她决定开口」，不是内容。**"""
    import json

    from gits.core.db import Writer

    ev, created = Writer(db).append_event(
        source="self", kind="initiative",
        ext_id=f"initiative:{actor}:{now}", ts=now, recv_ts=now,
        payload=json.dumps({"reason": reason, "candidate": candidate},
                           ensure_ascii=False).encode(),
        scope=f"self:{actor}")
    return ev if created else None
