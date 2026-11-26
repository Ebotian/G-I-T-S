"""额度账户的事件投影：**入账是规则，支出是事件。**

`gits/io/quota.py` 已经算清了"每天多少钱、怎么攒、什么时候清零"，但它一直
**只是个模型**——`Account.charge()` 只在测试里被调用过，生产里没有任何东西记账。
结果是"日上限"从来没生效过：主动开口每 20 分钟一次模型调用，唯一的刹车是
outbox 积压把 backlog 顶满——那是社会性刹车，不是钱上的刹车。

这个模块把账本接上电。两条口径：

    **入账是规则**    每天 +daily、封顶 cap、周一清零，全部由 now 推出来，
                      不落事件。落事件的话，每天一条，而它永远是 (daily, 星期几)
                      的函数——写多少条都一样。
    **支出是事件**    每一笔都落 `kind='spend'`，重放能重建，wipe 能清掉，
                      也能回答"这 30 天钱花在哪了"。

所以账户状态永远是 (现在几点, 本周的 spend 事件) 的一个纯函数。
**没有任何可写的字段。**
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from gits.core.db import DB, Writer

#: 每个实例的每日额度怎么来：月度预算 × 自主份额 / 天数 / 实例数。
#: 默认值在 `quota.Budget`，这里只读环境变量覆盖。
ENV_MONTHLY = "GITS_MONTHLY_CNY"
DEFAULT_MONTHLY = 200.0


def budget():
    """月度预算。**这是唯一的固定量**，其余全部由它派生。"""
    import os

    from gits.io.quota import Budget

    raw = os.environ.get(ENV_MONTHLY, "").strip()
    try:
        monthly = float(raw) if raw else DEFAULT_MONTHLY
    except ValueError:
        monthly = DEFAULT_MONTHLY
    return Budget(monthly_cny=max(0.0, monthly))


def allowance_for(actor: str):
    return __import__("gits.io.quota", fromlist=["allowance"]).allowance(
        budget(), actor)


def week_start(now: int) -> int:
    """本周一 00:00（本地时区）。**周日 24:00 清零 = 周一 00:00 重新开始。**"""
    d = dt.datetime.fromtimestamp(now / 1000)
    monday = (d - dt.timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp() * 1000)


def day_start(now: int) -> int:
    d = dt.datetime.fromtimestamp(now / 1000)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)


def spends(db: DB, *, actor: str, since: int, until: int | None = None) -> list[tuple[int, float, str]]:
    """[since, until) 之间的支出。**只有元数据，没有内容。**"""
    rows = db.q(
        "SELECT ts, payload FROM events WHERE kind='spend' AND ts>=?"
        " AND (? IS NULL OR ts<?) ORDER BY ts, id",
        (since, until, until))
    out = []
    for r in rows:
        try:
            obj = json.loads(r["payload"] or b"{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if obj.get("actor") != actor:
            continue
        out.append((int(r["ts"]), float(obj.get("cost") or 0.0),
                    str(obj.get("kind") or "")))
    return out


def state(db: DB, *, actor: str = "her", now: int | None = None) -> dict[str, Any]:
    """账户此刻的样子。**按规则重放一遍本周，不读任何字段。**

    重放的就是 `quota.Account` 的三条规则：每天入账封顶、日上限、
    周一清零。区别只是"支出"来自事件，而"入账"来自日历。
    """
    a = allowance_for(actor)
    b = budget()
    daily, cap = a.cny, a.cny * b.accrue_cap_days
    today = day_start(now) if now else day_start(int(dt.datetime.now().timestamp() * 1000))

    rows = spends(db, actor=actor, since=week_start(now))
    balance = 0.0
    spent_today = 0.0
    spent_week = 0.0
    # 从周一到今天，一天一天走。**周一总是先入账再花钱**，
    # 所以不会出现"余额 0 还要等到明天"。
    d = week_start(now)
    while d <= today:
        balance = min(balance + daily, cap)
        spent_today = 0.0
        for ts, cost, _k in rows:
            if d <= ts < d + 86_400_000:
                balance -= cost
                spent_today += cost
                spent_week += cost
        d += 86_400_000
    day_cap = daily * b.burst_factor
    return {"actor": actor, "daily": daily, "balance": round(balance, 6),
            "spent_today": round(spent_today, 6),
            "spent_week": round(spent_week, 6),
            "day_cap": round(day_cap, 6),
            "spendable": round(max(0.0, min(balance, day_cap - spent_today)), 6),
            "accrue_cap": round(cap, 6)}


def can_spend(db: DB, *, cost: float, actor: str = "her",
              now: int) -> tuple[bool, str]:
    """这一步花得起吗。**返回理由，不只是布尔。**"""
    s = state(db, actor=actor, now=now)
    left = s["spendable"]
    if cost > left + 1e-9:
        return (False, f"额度不够：还能花 {left:.4f} 元，这一步约 {cost:.4f} 元")
    return (True, f"额度够：{left:.4f} 元")


def charge(db: DB, *, actor: str, kind: str, cost: float, now: int,
           what: str = "", ref: str | None = None,
           units: float = 0.0) -> int | None:
    """记一笔支出。**这是账本唯一的写入口。**

    超支也照记：真花了钱就得记账，账本不能因为"超了"而假装没花。
    拦的是**下一笔**——`can_spend()` 在开口之前问一次。
    """
    cost = max(0.0, float(cost))
    if cost == 0.0 and units == 0.0:
        return None
    body = json.dumps({"actor": actor, "kind": kind, "cost": round(cost, 9),
                       "units": round(float(units), 3),
                       "what": what[:32]},
                      ensure_ascii=False, sort_keys=True)
    ev, _created = Writer(db).append_event(
        source="self", kind="spend",
        ext_id=f"spend:{actor}:{ref if ref is not None else f'{now}:{kind}'}",
        ts=now, recv_ts=now, payload=body.encode())
    return ev


def charge_usage(db: DB, *, actor: str, usage: dict, now: int,
                 what: str, ref: str | None = None) -> tuple[float, int | None]:
    """按 API 返回的 usage 记账。**账单以 usage 为准，不以估算为准。**"""
    from gits.speech.llm import cost_cny

    cost = cost_cny(usage or {})
    units = int((usage or {}).get("completion_tokens") or 0)
    return cost, charge(db, actor=actor, kind="llm", cost=cost, now=now,
                        what=what, ref=ref, units=units)


def statement(db: DB, *, actor: str = "her", now: int | None = None) -> str:
    """给人看的账户视图。**"像电子账户一样可见"。**"""
    from gits.io.quota import bar

    s = state(db, actor=actor, now=now)
    cap = s["day_cap"]
    pct = 0.0 if cap <= 0 else min(1.0, s["spent_today"] / cap)
    return "\n".join([
        f"额度账户  {s['actor']}",
        f"  余额      {s['balance']:.3f} 元   {bar(pct)}",
        f"  今日可花  {s['spendable']:.3f} 元"
        f"（日上限 {cap:.3f}，已花 {s['spent_today']:.3f}）",
        f"  每日入账  {s['daily']:.3f} 元 · 攒额上限 {s['accrue_cap']:.3f} 元"
        f" · 周一 00:00 清零",
        f"  本周累计  {s['spent_week']:.3f} 元",
    ])
