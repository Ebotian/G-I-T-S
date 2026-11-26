"""主动开口：**她先说一句，没有人问。**

这条路径以前是断的：gate.evaluate() 有决策逻辑，但**没有任何调用方**；
respond() 支持 proactive 参数，但没人传。决策机制齐了，没人按下按钮。

**没有频率闸，也没有时机闸。**

两样都不该是硬规则：

    频率   憋得越久压力越大、门槛越低；连续开口有 SEND_DECAY 衰减。
           这两项合起来就是频率——再加一个"每 3 小时一次"是把控制回路
           塞进策略层的东西，而它会在最该说话的时候拦住她。
    时机   他这个时段在不在是**预测**，按比例削弱价值（gate.presence），
           不是"低于 0.15 就不许说"。硬门槛会让新装的实例永远哑着，
           也会让"他今天凌晨醒着"这种真实情况说不上话。

剩下两道，是事实不是策略：

    出口   通道在线吗——断了就是断了
    价值   证据够不够（gate.evaluate）
"""

from __future__ import annotations

from typing import Any

from gits.core.db import DB


def spoken(db: DB, *, actor: str = "her") -> list[int]:
    """主动开口的时间戳，最近的在前。**压力靠它算。**"""
    return [int(r["ts"]) for r in db.q(
        "SELECT ts FROM events WHERE kind='initiative' AND scope=?"
        " ORDER BY ts DESC LIMIT 20", (f"self:{actor}",))]


def pressure(db: DB, *, now: int, actor: str = "her") -> float:
    """憋了多久：0 = 刚说过，1 = 憋到天花板。

    **只降门槛，不抬价值。** 它替代了原来的"每 3 小时一次"——
    刚说完时门槛高，自然就不会马上再说；憋久了门槛低，自然也就会说。
    """
    last = spoken(db, actor=actor)
    if not last:
        return 1.0
    ceiling = 86_400_000 / 2.0          # 一天两次的量级算憋满
    return max(0.0, min(1.0, (now - last[0]) / ceiling))


def presence(db: DB, *, now: int) -> float | None:
    """他这个时段在不在。**先问模型，没有模型就退到粗估。**

    `presence_rate` 要一份夜间拟合好的 GLM。没拟合之前它抛异常，
    那时退到一个不需要模型的东西：这个小时在过去出现过多少次消息，
    除以最忙的那个小时。**空数据返回 None**，由调用方决定怎么办——
    空白不该变成禁令，也不该被当成"他一定在"。
    """
    from gits.time import presence_rate

    try:
        return float(presence_rate(db, now))
    except Exception:
        pass
    rows = db.q("SELECT ts FROM events WHERE kind='msg_in'"
                " ORDER BY ts DESC LIMIT 2000")
    if not rows:
        return None
    import datetime as dt

    hour = dt.datetime.fromtimestamp(now / 1000).hour
    buckets = [0] * 24
    for r in rows:
        buckets[dt.datetime.fromtimestamp(int(r["ts"]) / 1000).hour] += 1
    top = max(buckets) or 1
    return buckets[hour] / top


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
    return GateInputs(
        channel_online=channel_online,
        unexcluded=float(cand.unexcluded), resolvable=float(cand.resolvable),
        backlog_pressure=backlog, consecutive=consec,
        confidence=conf, drive=mom, presence=presence(db, now=now))


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
    res = gate.evaluate(inputs, pressure=p)
    return {"allow": bool(res.allow), "why": res.reason,
            "value": round(float(res.value), 3),
            "pressure": round(p, 3),
            "presence": None if inputs.presence is None
            else round(inputs.presence, 2),
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
