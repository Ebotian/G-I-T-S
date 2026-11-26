"""关系记账：**数出来的，不是记下来的。**

`body_count` 这类字段不能是字段。理由和刚删掉的 `blood_type` 一样，只是更狠：

    字段可以被"总结"   —— 模型周更时说一句，值就变了
    计数器被改一次，就和事实永久分开
    而且它**不能减**   —— 减错了没有任何东西能改回来

所以这里的真相是事件，数字是投影：

    kind='relation'   action ∈ {meet, kiss, confess, intimacy, breakup}

三条硬规则：

    **只能加**     没有"把 body_count 改小"这个操作，只有"记一次"
    **增量恒为 1**  一条事件只代表一次；模型不能一次加 5
    **她不能自己记账**  src 必须是 'world'（他说了）或 'manual'（他明确给出）。
                  'dream'（模型自己想的）一律拒——这是
                  "系统不能把自己的输出当成世界的证据"的第六处。

第七条不变式（convergent.md）在这里的具体形状：**会计和叙事分开**。
她可以在句子里说任何话，但账本只认那五种动作。
"""

from __future__ import annotations

import json
from typing import Any

from gits.core.db import DB, Writer

#: 允许的动作。**枚举由代码定，模型只能从里面选**——
#: 让模型自由发挥，一周之后账本里会多出"半次""差点"。
ACTIONS = ("meet", "kiss", "confess", "intimacy", "breakup")

#: 计入 body_count 的动作。身体接触和数字，只有这一个算。
BODY_ACTIONS = frozenset({"intimacy"})

#: 谁说了算。'dream' 不在里面，这是故意的。
ALLOWED_SRC = frozenset({"world", "manual"})

#: 单用户的账：默认就是他。多人的话这里换成稳定身份。
DEFAULT_PARTNER = "他"

#: partner 名字的长度上限。长成一句话的东西不是身份。
MAX_PARTNER = 16


def _fault(db: DB, *, where: str, exc: BaseException, now: int) -> None:
    """静默失败要显示。按小时去重，不会刷屏。**和 Ghost.fault 同一个形状**，
    但这里的调用方不是 Ghost，所以单独给一个（不是复制粘贴的偷懒：
    Ghost.fault 里有 self.w，这里没有那个对象）。"""
    try:
        Writer(db).append_event(
            source="self", kind="fault",
            ext_id=f"fault:{where}:{now // 3_600_000}", ts=now, recv_ts=now,
            payload=(where + ": " + type(exc).__name__ + ": "
                     + str(exc))[:300].encode())
    except Exception:
        pass                        # 记失败本身不能再失败


def _payload(row: dict) -> dict[str, Any]:
    raw = row.get("payload")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def events(db: DB) -> list[dict[str, Any]]:
    """全部关系事件，按 (ts, id) 排。**ts 先、id 兜底**——
    同一毫秒里连记两条时，id 是唯一能定的顺序。"""
    rows = db.conn.execute(
        "SELECT id, ts, ext_id, payload FROM events WHERE kind='relation'"
        " ORDER BY ts, id").fetchall()
    out = []
    for r in rows:
        obj = _payload(dict(r))
        action = obj.get("action")
        if action not in ACTIONS:
            continue
        out.append({"id": r["id"], "ts": r["ts"], "action": action,
                    "partner": obj.get("partner") or DEFAULT_PARTNER,
                    "src": obj.get("src") or "world"})
    return out


def record(db: DB, *, action: str, now: int, partner: str = DEFAULT_PARTNER,
           src: str = "manual", ref: str | int | None = None) -> int | None:
    """记一次。**返回事件 id，被拒返回 None。**

    被拒的三种情况都落 fault：动作不在枚举里、来源是模型自己、名字不像名字。
    """
    action = (action or "").strip().lower()
    partner = (partner or DEFAULT_PARTNER).strip()[:MAX_PARTNER]
    bad = None
    if action not in ACTIONS:
        bad = f"动作不在枚举里：{action!r}"
    elif src not in ALLOWED_SRC:
        bad = f"来源 {src!r} 不许记账——她不能自己给自己记账"
    elif not partner:
        bad = "partner 是空的"
    if bad:
        _fault(db, where="relation", exc=ValueError(bad), now=now)
        return None

    body = json.dumps({"action": action, "partner": partner, "src": src},
                      ensure_ascii=False, sort_keys=True)
    ev, _created = Writer(db).append_event(
        source="self", kind="relation",
        ext_id=f"relation:{action}:{partner}:{ref if ref is not None else now}",
        ts=now, recv_ts=now, payload=body.encode())
    return ev


def baseline(db: DB) -> dict[str, Any]:
    """前史：认识他之前的部分。

    这部分**推不出来**——它不在事件里，因为那时还没有她。
    所以它是记录，不是投影；写一次之后不再变（后面的事件会接着往上加）。
    """
    row = db.one("SELECT payload FROM events WHERE kind='relation_seed'"
                 " ORDER BY id DESC LIMIT 1")
    return _payload(dict(row)) if row else {}


def seed(db: DB, *, now: int, body_count: int = 0,
         first_time: int | None = None) -> int | None:
    """写前史。**只该在导入时调一次。**"""
    body = json.dumps({"body_count": max(0, int(body_count)),
                       "first_time": int(first_time) if first_time else None},
                      ensure_ascii=False, sort_keys=True)
    ev, _created = Writer(db).append_event(
        source="self", kind="relation_seed", ext_id="relation_seed:0",
        ts=now, recv_ts=now, payload=body.encode())
    return ev


def counters(db: DB, *, now: int) -> dict[str, Any]:
    """数出来的那本账。**不落库**——它不是状态，是事件的一个函数。"""
    base = baseline(db)
    base_body = int(base.get("body_count") or 0)
    base_first = base.get("first_time")

    per: dict[str, int] = {a: 0 for a in ACTIONS}
    partners: set[str] = set()
    first_ts: int | None = int(base_first) if base_first else None
    last_ts: int | None = None

    for e in events(db):
        per[e["action"]] += 1
        if e["action"] in BODY_ACTIONS:
            # **同一毫秒的两条不会被合并成一条**：身份是 partner，不是时间。
            partners.add(e["partner"])
        if e["action"] in BODY_ACTIONS or e["action"] == "kiss":
            first_ts = e["ts"] if first_ts is None else min(first_ts, e["ts"])
        last_ts = e["ts"] if last_ts is None else max(last_ts, e["ts"])

    out: dict[str, Any] = {a: n for a, n in per.items() if n}
    body_count = base_body + len(partners)
    if body_count:
        out["body_count"] = body_count
    if first_ts:
        out["since_first_d"] = max(0, (now - first_ts) // 86_400_000)
    if last_ts:
        out["since_last_d"] = max(0, (now - last_ts) // 86_400_000)
    return out


def brief(db: DB, *, now: int) -> str:
    """给上下文的那一行。**数字，不是句子。**

    空账返回空串——没有发生的事不该占 token，也不该被模型补出来。
    """
    c = counters(db, now=now)
    if not c:
        return ""
    return json.dumps(c, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
