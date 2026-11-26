"""背景的分类详细：**按需取，不常驻。**

常驻的只有 brief 那一小块（可核对的常识）。详细的东西分门别类，
模型想知道更多时才取——因为一次工具调用是一次完整的上下文重发，
比 22 轮的常驻成本还贵。

每个工具返回 **dict**，不是句子。说什么由模型决定。

分类的依据是「他可能问哪一类问题」：

    where    他今天去过哪、待了多久        "他一整天都在家？"
    outside  外面什么天、明天怎么样        "你那边冷吗"
    place    某个地方是什么样              "公司那边几点下班"
    self     她此刻的状态（情绪/在不在意）  "你今天怎么话这么少"
"""

from __future__ import annotations

from typing import Any, Callable

from gits.core.db import DB


def _where(db: DB, *, now: int, days: int = 1, **_: Any) -> dict[str, Any]:
    """他最近的移动：[(场所, 待了多久, 什么时候)]。"""
    from gits.world import places

    out = []
    for p in places.real_places(db):
        hours = places.profile(db, int(p["id"]))
        out.append({
            "place": places.name_of(p) or None,
            "n": int(p["n"]),
            "stay_min": int(p["best_dwell_ms"] / 60_000),
            "hours": [h for h, c in enumerate(hours) if c > 0][:12],
        })
    cur = places.current(db)
    return {"now": (places.name_of(cur) or None) if cur else None,
            "places": out[:8]}


def _outside(db: DB, *, now: int, **_: Any) -> dict[str, Any]:
    """此刻的天气 + 窗口内的预报。"""
    from gits.world import amap
    from gits.world import forecast as F

    r = amap.latest(db, now=now)
    out: dict[str, Any] = {}
    if r and not amap._stale(r, now=now, ttl=amap.FRESH_MS):
        out["weather"] = r.get("weather")
        out["temp"] = r.get("temp")
        out["report"] = r.get("report")
    fc = F.latest(db)
    if fc:
        today = F._date(now)
        out["forecast"] = [
            {"date": c.get("date"), "day": c.get("dayweather"),
             "night": c.get("nightweather"),
             "lo": c.get("nighttemp"), "hi": c.get("daytemp")}
            for c in (fc.get("casts") or [])
            if (F._days_between(today, c.get("date") or "") or 0) >= 0
        ][:3]
    return out


def _place(db: DB, *, now: int, place_id: int | None = None,
           name: str | None = None, **_: Any) -> dict[str, Any]:
    """某个地方：别名、常态时段、最近去过。"""
    from gits.world import places

    target = None
    for p in places.all_places(db):
        if place_id is not None and int(p["id"]) == int(place_id):
            target = p
        elif name and places.name_of(p) == name:
            target = p
    if target is None:
        return {}
    hours = places.profile(db, int(target["id"]))
    return {
        "name": places.name_of(target) or None,
        "n": int(target["n"]),
        "best_stay_min": int(target["best_dwell_ms"] / 60_000),
        "last_seen": int(target["last_ts"]),
        "hours": [h for h, c in enumerate(hours) if c > 0],
    }


def _self(db: DB, *, now: int, actor: str = "her", **_: Any) -> dict[str, Any]:
    """她自己的状态。**说分布，不说结论。**"""
    from gits.social import affect

    d = affect.distribution(db, actor, now=now)
    top = d.get("top")
    return {"mood": (affect.ZH.get(top, top[0]) if top else None),
            "clarity": round(float(d.get("clarity") or 0), 2),
            "n": round(float(d.get("n") or 0), 1)}


#: 工具目录。**分类是给模型看的**，所以每个都带一句人话的作用说明。
CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "where", "run": _where,
     "about": "他最近在哪些地方、待了多久"},
    {"name": "outside", "run": _outside,
     "about": "外面什么天气、接下来两天怎么样"},
    {"name": "place", "run": _place,
     "about": "某个地方的情况（需要 place_id 或名字）"},
    {"name": "self", "run": _self,
     "about": "她自己此刻的情绪分布"},
)

_BY_NAME: dict[str, Callable[..., dict]] = {t["name"]: t["run"] for t in CATALOG}


def detail(db: DB, kind: str, *, now: int, **kw: Any) -> dict[str, Any]:
    """取某一类的详细。**未知分类返回空，不抛。**"""
    fn = _BY_NAME.get(kind)
    if fn is None:
        return {}
    try:
        return fn(db, now=now, **kw)
    except Exception:
        return {}
