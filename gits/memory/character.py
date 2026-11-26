"""角色事实：年龄、身高、体重……**是记录，不是记忆。**

三类东西在这里分工：

    记忆   会被经历推翻，会衰减（nodes）
    记录   不会被经历推翻（facts 的生日、这里的角色事实）
    读数   世界此刻的样子，会过期（weather / loc）

## 年龄不写，推导

"年龄每天自动更新"不需要每天写一条记录——存生日，读的时候算。
写的话每天一条事件，而它永远是 birth_date 的函数，写多少条都一样。

## 身高体重周更，但判定在代码里

每一个数值变化都要过一道**机械的增减量判定**：

    模型说"这周长高了 8 厘米"   →  拒收，保留旧值，落一条 fault
    模型说"体重多了 0.4 公斤"   →  收下

判定放在代码里而不是提示词里，理由和别处一样：**提示词是建议，代码是约束。**
"""

from __future__ import annotations

import json
from typing import Any, Callable

from gits.core.db import DB, Writer

#: 数值型事实的单次变化上限。**超了就拒收，不是夹住**——
#: 夹住会让一个荒唐的数字变成"稍微有点荒唐"，而拒收保留了旧值。
BOUNDS: dict[str, float] = {
    "height_cm": 0.6,       # 一周长高超过 0.6cm 不是成长，是笔误
    "weight_kg": 1.5,       # 一周体重浮动超过 1.5kg 不是生活，是笔误
}
#: 这些键允许存在。名字固定，免得模型的自由发挥变成一堆散字段。
ALLOWED = frozenset({"birth_date", "height_cm", "weight_kg", "gender",
                     "eye_color", "hair_color", "shoe_size"})
#: 每周最多更新几次（防止 dream 跑两遍就多写一份）


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def current(db: DB) -> dict[str, str]:
    return {str(r["key"]): str(r["value"])
            for r in db.q("SELECT key, value FROM character")}


def age(db: DB, *, now: int) -> int | None:
    """年龄由生日推导。**每天自动更新，不需要写。**"""
    bd = current(db).get("birth_date")
    if not bd:
        return None
    import datetime as dt

    try:
        y, m, d = (int(x) for x in bd.split("-")[:3])
    except (ValueError, AttributeError):
        return None
    today = dt.datetime.fromtimestamp(now / 1000).date()
    return today.year - y - (1 if (today.month, today.day) < (m, d) else 0)


def record(db: DB, *, key: str, value: Any, src: str = "seed",
           now: int) -> int | None:
    """落一条角色事实。**走事件**，重放能重建。"""
    key = (key or "").strip()
    if key not in ALLOWED:
        return None
    text = str(value).strip()
    if not text:
        return None
    ev, created = Writer(db).append_event(
        source="self", kind="character",
        ext_id=f"character:{key}:{text}:{now}", ts=now, recv_ts=now,
        payload=json.dumps({"key": key, "value": text, "src": src},
                           ensure_ascii=False).encode())
    if created:
        from gits.core import project

        row = db.one("SELECT * FROM events WHERE id=?", (ev,))
        with Writer(db).txn() as c:
            project._apply(c, row, json.dumps({"key": key, "value": text,
                                               "src": src},
                                              ensure_ascii=False))
    return ev


def bounded(db: DB, *, key: str, value: Any) -> tuple[bool, str]:
    """机械判定：这个变化可以接受吗。**返回理由，不只是布尔。**"""
    old = current(db).get(key)
    limit = BOUNDS.get(key)
    if limit is None:
        return (True, "无上限的键")
    new_n = _num(value)
    if new_n is None:
        return (False, f"{key} 不是数字：{value!r}")
    old_n = _num(old)
    if old_n is None:
        return (True, "还没有旧值，先收下")
    delta = abs(new_n - old_n)
    if delta > limit:
        return (False, f"{key} 变化 {delta:.2f} 超过上限 {limit}")
    return (True, f"{key} 变化 {delta:.2f}，在上限内")


def line(db: DB, *, now: int) -> str:
    """给上下文的那一小块。**只在上下文开头出现一次。**

    给数值，不给句子——她怎么说这些事实是她的事。
    """
    cur = current(db)
    out: dict[str, Any] = {}
    a = age(db, now=now)
    if a is not None:
        out["age"] = a
    for k in ("height_cm", "weight_kg", "gender", "eye_color",
              "hair_color", "shoe_size"):
        if k in cur:
            n = _num(cur[k])
            out[k] = (int(n) if n is not None and n == int(n) else n) \
                if n is not None else cur[k]
    if not out:
        return ""
    return json.dumps(out, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def week_of(now: int) -> str:
    """周编号。每周只更新一次，靠它判重。"""
    import datetime as dt

    d = dt.datetime.fromtimestamp(now / 1000)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def due_weekly(db: DB, *, now: int) -> bool:
    """该不该跑周更。**周日夜里**，且这一周还没跑过。"""
    import datetime as dt

    d = dt.datetime.fromtimestamp(now / 1000)
    if d.weekday() != 6 or d.hour < 20:
        return False
    key = f"character-run:{week_of(now)}"
    return db.scalar("SELECT count(*) FROM events WHERE ext_id=?",
                     (key,)) == 0


def mark_week(db: DB, *, now: int) -> int | None:
    """标记这一周跑过了。**没有它 due_weekly 永远为真**，
    同一周会重复调模型、重复花钱。"""
    ev, _created = Writer(db).append_event(
        source="self", kind="character_run",
        ext_id=f"character-run:{week_of(now)}", ts=now, recv_ts=now,
        payload=b"done")
    return ev
