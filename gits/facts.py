"""固定事实：生日、纪念日、截止日。

**它们不是记忆，是记录。**

    能被经历推翻的          → 记忆（会衰减、会被顶掉、会进漏斗）
    不可能被经历推翻的      → 记录（不衰减，只有人/外部能改）

这不是特殊化。「他在意别人怎么看他的工作」会被一次反例推翻；
「他的生日是 3 月 12 日」不会——只有他本人能改。两类对象，两套规则。

如果生日存成普通记忆节点，结果是可以预先算出来的：
364 天没人提，r 掉到 0，第 365 天她想不起来。**遗忘机制恰好在那一天把它忘掉。**

平时它们完全不进上下文；只在窗口内由渲染层给一行**原料**：

    「三天后是他的生日。」

不是「你应该祝他生日快乐」。说什么是她的事——
否则我们做的就不是同伴，是日历。
"""

from __future__ import annotations

import datetime as _dt

from gits.db import DB, Writer, now_ms
from gits.scope import self_of

#: 和 rhythm.py 同一个时区基准
TZ_OFFSET_HOURS = 8
#: 单条事实最多提前多少天进入上下文
MAX_LEAD = 30
#: 支持的日历。**lunar 会被明确拒绝，不猜。**
CALENDARS = {"solar"}


def _local(ts_ms: int) -> _dt.date:
    # 固定时区：日期判断不能跟着机器走，否则同一时刻在两台机器上是两天
    return _dt.datetime.fromtimestamp(
        ts_ms / 1000, _dt.timezone(_dt.timedelta(hours=TZ_OFFSET_HOURS))
    ).date()


def _this_year_date(year: int, month: int, day: int) -> _dt.date:
    try:
        return _dt.date(year, month, day)
    except ValueError:
        # 2/29 落在平年：按 3/1 算，且这是刻意的——比"跳过这一年"好，
        # 也比"报错让人重填"好（四年一次的生日不该每年都出问题）。
        if month == 2 and day == 29:
            return _dt.date(year, 3, 1)
        raise


def days_until(month: int, day: int, today: _dt.date) -> int:
    """离下一次这个日子还有几天。今天就是则 0。"""
    for year in (today.year, today.year + 1):
        d = _this_year_date(year, month, day)
        if d >= today:
            return (d - today).days
    return 365


def due(db: DB, now: int, *, tz_offset_hours: int = TZ_OFFSET_HOURS
        ) -> list[tuple[dict, int]]:
    """现在该进上下文的事实：[(事实, 还有几天)]。**平时返回空。**"""
    today = _local(now)
    out = [
        (dict(r), 0) for r in []
    ]
    for r in db.q("SELECT * FROM facts WHERE superseded_by IS NULL"
                  " ORDER BY month, day"):
        fact = dict(r)
        if fact["repeat"] == "once" and fact["year"] is not None:
            when = _this_year_date(int(fact["year"]), int(fact["month"]),
                                   int(fact["day"]))
            left = (when - today).days
            if 0 <= left <= int(fact["lead_days"]):
                out.append((fact, left))
            continue
        left = days_until(int(fact["month"]), int(fact["day"]), today)
        if left <= int(fact["lead_days"]):
            out.append((fact, left))
    out.sort(key=lambda f: f[1])
    return out


def lines(db: DB, now: int) -> tuple[str, ...]:
    """C 段里那几行。**只给事实，不给建议。**"""
    out = []
    for fact, left in due(db, now):
        label = str(fact["label"])
        if left == 0:
            out.append(f"今天是{label}。")
        elif left == 1:
            out.append(f"明天是{label}。")
        else:
            out.append(f"{left} 天后是{label}。")
    return tuple(out)


def add(db: DB, *, subject: str, key: str, label: str, month: int, day: int,
        year: int | None = None, lead_days: int = 0, repeat: str = "yearly",
        now: int | None = None) -> int:
    """记一条事实。走事件 → 投影，和别的记忆同一条重放路径。

    改生日 = 再写一条同 (subject, key) 的，旧的自动被顶掉。不删、不清。
    """
    from gits.project import fact_id

    now = now or now_ms()
    if repeat not in ("yearly", "once"):
        raise ValueError(f"repeat 只能是 yearly / once，拿到 {repeat!r}")
    _this_year_date(2000, month, day)          # 借闰年校验 2/29 合法
    payload = {"subject": subject, "key": key, "label": label,
               "month": month, "day": day, "year": year,
               "lead_days": lead_days, "repeat": repeat}
    import json

    ev, _ = Writer(db).append_event(
        source="fact", kind="fact", ext_id=fact_id(subject, key, now),
        ts=now, recv_ts=now, scope=self_of(subject),
        payload=json.dumps(payload, ensure_ascii=False).encode())
    from gits import project

    row = {"kind": "fact", "source": "fact",
           "ext_id": fact_id(subject, key, now), "ts": now,
           "scope": self_of(subject)}
    with Writer(db).txn() as c:
        project._apply(c, row, json.dumps(payload, ensure_ascii=False))
    return ev
