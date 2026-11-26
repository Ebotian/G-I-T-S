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

#: 身高年增长速度的上限，按年龄分段（cm/年）。
#:
#: 来源（都是临床口径，不是我自己拍的）：
#:   生后第一年 25cm；第二年 10-12cm；2 岁到青春期前 5-7cm/年；
#:   青春期突增平均 10cm/年（持续 1-2 年）；骨骺闭合后停止增长。
#:   见北京儿童医院《青春期前及青春期生长规律》、ScienceDirect 儿科生长速度章节。
#:
#: **这是上限，不是目标**——它拦的是"模型说这周长高了 8 厘米"，
#: 不是规定她该长多快。
GROWTH_CM_PER_YEAR: tuple[tuple[float, float, float], ...] = (
    (0, 1, 25.0),
    (1, 2, 11.0),
    (2, 10, 7.0),
    (10, 18, 10.0),      # 覆盖青春期突增
    (18, 25, 1.0),
    (25, 999, 0.2),      # 成年后基本不长，留一点测量误差
)

#: 体重年变化上限（kg/年）。成人给得宽——衣服、水分、肌肉都能变，
#: 这里拦的同样是笔误，不是生活方式。
WEIGHT_KG_PER_YEAR: tuple[tuple[float, float, float], ...] = (
    (0, 1, 6.0),
    (1, 3, 2.5),
    (3, 18, 3.5),
    (18, 999, 6.0),
)

#: 围度年变化上限（cm/年）。青春期在发育，成年后只随体重浮动。
GIRTH_CM_PER_YEAR: tuple[tuple[float, float, float], ...] = (
    (0, 10, 6.0),
    (10, 18, 8.0),       # 青春期胸部/臀围的变化比身高更集中
    (18, 25, 4.0),
    (25, 999, 3.0),      # 成年后跟着体重走
)

#: 围度键。这些用同一张表判定。
GIRTH_KEYS = frozenset({"bust_cm", "waist_cm", "hip_cm", "underbust_cm",
                        "shoulder_cm", "inseam_cm"})

#: 没有年龄可依的键，用固定上限兜底。
BOUNDS: dict[str, float] = {}

#: **身高单调不减。** 青春期之后它只会持平，任何下降都是错。
#: （真实的老年身高缩水不在这里处理——她不是老人，而多一条规则就多一个会静默失效的地方。）
MONOTONIC = frozenset({"height_cm"})
#: 这些键允许存在。名字固定，免得模型的自由发挥变成一堆散字段。
#:
#: **只放可测量的。** 身高、体重、三围这类能用尺子和秤核对的。
#: 血型、色觉、性别这些不是"每周会变的状态"，是一条不该被模型改写的记录——
#: 放进周更里，模型"总结"一下就能改掉，那不是记录该有的样子。
ALLOWED = frozenset({
    "birth_date",
    # 整体
    "height_cm", "weight_kg",
    # 三围 + 下胸围（下胸围是罩杯的分母，没有它胸围只是个孤立数字）
    "bust_cm", "underbust_cm", "waist_cm", "hip_cm",
    # 骨架与四肢比例——游戏建模里决定剪影的那几项
    "shoulder_cm",      # 肩宽
    "inseam_cm",        # 内缝长（裆到脚踝），腿身比的分子
    "foot_cm",          # 脚长，鞋码由它推导
})

#: **每个事实多久更新一次，怎么更新。**
#:
#: "不同的状态不同的时序"就写在这一张表里——不要散在 if 里。
#: 三种节奏：
#:
#:     derived   读的时候算，不存（年龄）
#:     weekly    周日夜里由模型总结性更新（身高体重这类会慢变的量）
#:     manual    只在明确给出时写（血型、色觉——它们不会变，也不该被"总结"）
CADENCE: dict[str, str] = {
    "birth_date": "manual",
    "height_cm": "weekly",
    "weight_kg": "weekly",
    "bust_cm": "weekly",
    "waist_cm": "weekly",
    "hip_cm": "weekly",
    "underbust_cm": "weekly",
    "shoulder_cm": "weekly",
    "inseam_cm": "weekly",
    "foot_cm": "manual",     # 成年后不再变，周更去"总结"它只会出错
}

#: 周更时允许模型动的键。**manual 的一律不许它改**——
#: 血型被"总结"成别的，不是生活变化，是错误。
WEEKLY_KEYS = frozenset(k for k, v in CADENCE.items() if v == "weekly")
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


def _per_week(table: tuple[tuple[float, float, float], ...],
               age: float | None, *, default: float, floor: float) -> float:
    """按年龄取年速度，换成一周的上限。

    `floor` 是绝对下限：成年后按年速度折算出来只有 0.006，
    但测量误差、站姿、早晚差异都在这量级——**拦的是笔误，不是生理极限。**
    """
    if age is None:
        return default                # 不知道年龄：宽一点，别把正常变化也拦了
    for lo, hi, per_year in table:
        if lo <= age < hi:
            return max(per_year / 52.0 * 1.5, floor)
    return default


def bounded(db: DB, *, key: str, value: Any, now: int) -> tuple[bool, str]:
    """机械判定：这个变化可以接受吗。**返回理由，不只是布尔。**

    上限按**年龄**取：一岁和二十五岁，一周能变多少完全不是一回事。
    身高额外要求**单调不减**。
    """
    old = current(db).get(key)
    new_n = _num(value)
    if (key in ("height_cm", "weight_kg") or key in GIRTH_KEYS)             and new_n is None:
        return (False, f"{key} 不是数字：{value!r}")
    if new_n is None:
        return (True, "非数值键")
    old_n = _num(old)
    if old_n is None:
        return (True, "还没有旧值，先收下")

    delta = new_n - old_n
    if key in MONOTONIC and delta < 0:
        return (False, f"{key} 只能不降，这次是 {delta:+.2f}")

    a = age(db, now=now)
    if key == "height_cm":
        limit = _per_week(GROWTH_CM_PER_YEAR, a, default=0.5, floor=0.3)
    elif key == "weight_kg":
        limit = _per_week(WEIGHT_KG_PER_YEAR, a, default=2.0, floor=1.0)
    elif key in GIRTH_KEYS:
        limit = _per_week(GIRTH_CM_PER_YEAR, a, default=1.5, floor=1.0)
    else:
        limit = BOUNDS.get(key)
    if limit is None:
        return (True, "无上限的键")
    if abs(delta) > limit:
        return (False, f"{key} 变化 {delta:+.2f} 超过上限 {limit:.2f}"
                f"（{('年龄 %.0f 岁' % a) if a is not None else '没有生日'}）")
    where = f"（{'年龄 %.0f 岁' % a if a is not None else '没有生日'}）"
    return (True, f"{key} 变化 {delta:+.2f}，在上限 {limit:.2f} 内{where}")


def line(db: DB, *, now: int) -> str:
    """给上下文的那一小块。**只在上下文开头出现一次。**

    给数值，不给句子——她怎么说这些事实是她的事。
    """
    cur = current(db)
    out: dict[str, Any] = {}
    a = age(db, now=now)
    if a is not None:
        out["age"] = a
    for k in ("height_cm", "weight_kg", "bust_cm", "waist_cm",
              "hip_cm", "underbust_cm"):
        if k in cur:
            n = _num(cur[k])
            out[k] = (int(n) if n is not None and n == int(n) else n) \
                if n is not None else cur[k]
    # 派生量一起给：她知道自己 75B，比知道自己胸围 86 更自然。
    out.update({k: v for k, v in derived(db, now=now).items()
                if k not in out})
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

# ── 派生量：读的时候算，不存 ────────────────────────────────

#: 罩杯差值 -> 字母。差值是中国/日本口径（上胸围 − 下胸围，单位 cm）。
#: 这是**分档**，不是尺寸本身——尺寸由下胸围给（如 75B = 下胸围 75、差值 B）。
CUP_STEPS: tuple[tuple[float, str], ...] = (
    (7.5, "AA"), (10.0, "A"), (12.5, "B"), (15.0, "C"), (17.5, "D"),
    (20.0, "E"), (22.5, "F"), (25.0, "G"), (999, "H+"),
)


def _f(d: dict[str, str], key: str) -> float | None:
    return _num(d.get(key))


def derived(db: DB, *, now: int) -> dict[str, Any]:
    """由已有事实算出来、**不落库**的几个量。"""
    cur = current(db)
    out: dict[str, Any] = {}
    h = _f(cur, "height_cm")
    w = _f(cur, "weight_kg")
    bust, under = _f(cur, "bust_cm"), _f(cur, "underbust_cm")
    waist, hip = _f(cur, "waist_cm"), _f(cur, "hip_cm")
    if h and w and h > 0:
        out["bmi"] = round(w / ((h / 100.0) ** 2), 1)
    if bust and under and bust > under:
        diff = bust - under
        for edge, letter in CUP_STEPS:
            if diff < edge:
                out["cup"] = letter
                break
        out["band"] = int(round(under / 2.5) * 2.5)
    if waist and hip and hip > 0:
        out["whr"] = round(waist / hip, 3)
    inseam = _f(cur, "inseam_cm")
    if h and inseam and h > 0:
        out["leg_ratio"] = round(inseam / h, 3)
    foot = _f(cur, "foot_cm")
    if foot:
        # 中国鞋码 = 脚长(cm) × 2 − 10，四舍五入到半码
        out["shoe_cn"] = round((foot * 2.0 - 10.0) * 2) / 2
    return out


# ── 自洽检查 ────────────────────────────────────────────────

#: 参考区间。**是合理性包络，不是医学标准**——用来发现"身高体重组合不真实"这类错误，
#: 不是用来规定她该长什么样。年轻人中国女性的公开体型资料大致落在这个范围。
PLAUSIBLE = {
    "bmi": (15.0, 32.0),          # 低于/高于这个，身高体重自相矛盾
    "whr": (0.62, 0.95),          # 腰臀比
    "leg_ratio": (0.42, 0.55),    # 内缝长 / 身高
    "hip_over_height": (0.48, 0.62),
    "shoulder_over_height": (0.20, 0.27),
    "cup_diff": (2.5, 30.0),      # 上胸围 − 下胸围
}


def problems(values: dict[str, str] | None = None, *,
             now: int, db: DB | None = None) -> list[str]:
    """这套数字自相矛盾吗。**返回问题清单，空表示自洽。**

    检查的是**跨字段**的关系——单个字段的范围由 `bounded()` 管，
    这里管"每个都合理，合起来不可能"。
    """
    if values is None:
        if db is None:
            return []
        values = current(db)
    out: list[str] = []
    h = _num(values.get("height_cm"))
    w = _num(values.get("weight_kg"))
    bust, under = _num(values.get("bust_cm")), _num(values.get("underbust_cm"))
    waist, hip = _num(values.get("waist_cm")), _num(values.get("hip_cm"))
    shoulder = _num(values.get("shoulder_cm"))
    inseam = _num(values.get("inseam_cm"))

    if bust is not None and under is not None:
        if bust <= under:
            out.append(f"胸围 {bust:g} 不大于下胸围 {under:g}——这两个数互换了吧")
        else:
            lo, hi = PLAUSIBLE["cup_diff"]
            if not (lo <= bust - under <= hi):
                out.append(f"胸围差 {bust - under:g} 超出 {lo}-{hi}")
    if waist is not None and hip is not None and hip > 0:
        if waist >= hip:
            out.append(f"腰围 {waist:g} 不小于臀围 {hip:g}")
        else:
            lo, hi = PLAUSIBLE["whr"]
            if not (lo <= waist / hip <= hi):
                out.append(f"腰臀比 {waist / hip:.2f} 超出 {lo}-{hi}")
    if h and w and h > 0:
        b = w / ((h / 100.0) ** 2)
        lo, hi = PLAUSIBLE["bmi"]
        if not (lo <= b <= hi):
            out.append(f"BMI {b:.1f} 超出 {lo}-{hi}——身高体重自相矛盾")
    if h and hip:
        lo, hi = PLAUSIBLE["hip_over_height"]
        if not (lo <= hip / h <= hi):
            out.append(f"臀围/身高 {hip / h:.2f} 超出 {lo}-{hi}")
    if h and shoulder:
        lo, hi = PLAUSIBLE["shoulder_over_height"]
        if not (lo <= shoulder / h <= hi):
            out.append(f"肩宽/身高 {shoulder / h:.2f} 超出 {lo}-{hi}")
    if h and inseam:
        lo, hi = PLAUSIBLE["leg_ratio"]
        if not (lo <= inseam / h <= hi):
            out.append(f"内缝长/身高 {inseam / h:.2f} 超出 {lo}-{hi}")
    return out
