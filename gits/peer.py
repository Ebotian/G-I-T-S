"""实例之间：上限、回合、投递、观测。

这一层只做机制，不含任何内容。四条对应 peers.md 里已经定下的规则：

  上限    主动 ≤6 条/天/对，钱 ≤20%/对；**回应不占额度**
  回合    回应一定有回合，但内容可以是"没什么想说的"（沉默也是回应）
  投递    消息进**接收方的私有域**，同时进发送方的私有域；第三方不知道
  观测    没有选项可见性，"没发生"就不是数据，只是没接线

单向性很关键：A 说给 B，A 的记忆里是「我说过」，B 的记忆里是「他跟我说过」，
C 完全不知道这件事发生过——这才是真实的社会结构。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gits import actors
from gits.db import DB, Writer, now_ms
from gits.scope import self_of

DAY = 86_400_000
#: 每对每天主动上限。**只设上限，不设下限**
DAILY_COUNT = 6
#: 每对每天的钱上限（占个人日额度的比例）
MONEY_SHARE = 0.20
#: 草稿队列上限。没有它，昨天的情绪会占满今天的额度
MAX_DRAFTS = 2
#: 草稿有效期
DRAFT_TTL = DAY
#: 模型说"没什么想说的"时用的哨兵。**故意不像话**，免得被念出来
PASS = "<pass>"


@dataclass
class Sent:
    outbox_id: int
    drafted: bool = False


def _window(now: int) -> int:
    """滚动 24 小时，不是自然日。

    自然日有个难看的边界：23:59 说满 6 句、0:01 再说 6 句。
    滚动窗口没有这个缝，也不需要为"几点算新的一天"立规矩。
    """
    return now - DAY


def count(db: DB, frm: str, to: str, *, now: int) -> int:
    return int(db.scalar(
        "SELECT count(*) FROM outbox WHERE channel='peer'"
        " AND json_extract(meta, '$.from')=? AND json_extract(meta, '$.to')=?"
        " AND ts > ?", (frm, to, _window(now))) or 0)


def spent(db: DB, frm: str, to: str, *, now: int) -> float:
    rows = db.q(
        "SELECT usage_json, meta FROM model_calls WHERE kind IN"
        " ('peer_say','peer_reply') AND ts > ?", (_window(now),))
    total = 0.0
    for r in rows:
        try:
            m = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            continue
        if (m.get("from"), m.get("to")) != (frm, to):
            continue
        try:
            total += float(json.loads(r["usage_json"] or "{}").get("cost") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return total


def can_initiate(db: DB, frm: str, to: str, *, now: int | None = None,
                 daily: float = 0.667) -> tuple[bool, str]:
    now = now or now_ms()
    n = count(db, frm, to, now=now)
    if n >= DAILY_COUNT:
        return False, f"今天已经跟 {to} 说了 {n} 句，上限 {DAILY_COUNT}"
    cap = daily * MONEY_SHARE
    used = spent(db, frm, to, now=now)
    if used >= cap:
        return False, f"这一对今天花了 {used:.4f} 元，上限 {cap:.4f}"
    return True, "ok"


def drafts(db: DB, actor: str, *, now: int | None = None) -> list[dict]:
    now = now or now_ms()
    return [dict(r) for r in db.q(
        "SELECT * FROM peer_drafts WHERE actor=? AND ts > ? ORDER BY ts",
        (actor, _window(now)))]


def draft(db: DB, actor: str, to: str, text: str, *, now: int | None = None) -> bool:
    """封顶之后的话不下地狱，也不自动延期。**队列有条数上限。**"""
    now = now or now_ms()
    rows = drafts(db, actor, now=now)
    while len(rows) >= MAX_DRAFTS:
        oldest = rows.pop(0)
        with Writer(db).txn() as c:
            c.execute("DELETE FROM peer_drafts WHERE id=?", (oldest["id"],))
        # 过期不是消失，是转成"想说的话没说"——它本身就是经历
        Writer(db).append_event(
            source="peer", kind="unsaid", ext_id=f"unsaid-{oldest['id']}",
            ts=now, recv_ts=now, scope=self_of(actor),
            payload=str(oldest["text"]).encode())
    with Writer(db).txn() as c:
        c.execute("INSERT INTO peer_drafts(actor, to_actor, text, ts)"
                  " VALUES(?,?,?,?)", (actor, to, text, now))
    return True


def say(db: DB, frm: str, to: str, text: str, *, now: int | None = None,
        cost: float = 0.0, free: bool = False, reply_to: dict | None = None,
        daily: float = 0.667) -> Sent:
    """开口。**不占额度的只有回应**（`free=True`），主动一律先过上限。

    `reply_to` 是收到的那个回合。回应要**当场把回合用掉**——
    否则在真正投递之前，它会以为自己还有话没说完，于是回两遍。
    """
    now = now or now_ms()
    if reply_to is not None:
        Writer(db).append_event(
            source=f"peer:{frm}", kind="peer_reply_to",
            ext_id=str(reply_to["ext_id"]), ts=now, recv_ts=now,
            scope=self_of(frm), payload=b"")
    if not free:
        ok, why = can_initiate(db, frm, to, now=now, daily=daily)
        if not ok:
            draft(db, frm, to, text, now=now)
            return Sent(outbox_id=0, drafted=True)
    Writer(db).append_model_call(
        kind="peer_reply" if free else "peer_say", model="-", response=text,
        usage_json=json.dumps({"cost": round(cost, 6)}),
        meta=json.dumps({"from": frm, "to": to, "free": free}), ts=now)
    with Writer(db).txn() as c:
        oid = int(c.execute(
            "INSERT INTO outbox(ts, channel, text, status, meta)"
            " VALUES(?,?,?,'pending',?)",
            (now, "peer", text,
             json.dumps({"from": frm, "to": to, "free": free},
                        ensure_ascii=False))).lastrowid)
    return Sent(outbox_id=oid)


def deliver(db: DB, *, now: int | None = None) -> int:
    """把 peer 的 pending 落到双方各自的私有域里。**第三方什么都看不到。**"""
    now = now or now_ms()
    n = 0
    for row in db.q("SELECT * FROM outbox WHERE channel='peer'"
                    " AND status='pending' ORDER BY id LIMIT 20"):
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        frm, to = str(meta.get("from", "")), str(meta.get("to", ""))
        if not (frm and to):
            continue
        text = str(row["text"])
        Writer(db).append_event(source=f"peer:{frm}", kind="msg_in",
                                ext_id=f"peer-outbox:{row['id']}",
                                ts=now, recv_ts=now, payload=text.encode(),
                                scope=self_of(to))
        Writer(db).append_event(source=f"peer:{frm}", kind="msg_out",
                                ext_id=f"peer-sent:{row['id']}",
                                ts=now, recv_ts=now, payload=text.encode(),
                                scope=self_of(frm))
        with Writer(db).txn() as c:
            c.execute("UPDATE outbox SET status='sent', meta=?"
                      " WHERE id=?", (row["meta"], row["id"]))
        n += 1
    return n


def received(db: DB, actor: str, *, limit: int = 5) -> list[dict]:
    """轮到它说话的回合。**只含没处理过的**（有回应或有沉默记录就不算）。"""
    return [dict(r) for r in db.q(
        "SELECT * FROM events e WHERE e.kind='msg_in' AND e.scope=?"
        " AND e.source LIKE 'peer:%'"
        " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.ext_id=e.ext_id"
        "                 AND r.kind IN ('peer_silent','peer_reply_to'))"
        " ORDER BY e.id LIMIT ?", (self_of(actor), limit))]


def silent(db: DB, actor: str, event: dict, *, now: int | None = None) -> int:
    """这一回合用了，但没什么想说的。**沉默也是回应**——它不违约。"""
    now = now or now_ms()
    ev, _ = Writer(db).append_event(
        source=f"peer:{actor}", kind="peer_silent",
        ext_id=str(event["ext_id"]), ts=now, recv_ts=now,
        scope=self_of(actor), payload=b"")
    return ev


def affordance(db: DB, actor: str, *, now: int | None = None) -> list[str]:
    """选项**看得见**才算选项。每次露出都记一笔，否则"没发生"没法解释。"""
    now = now or now_ms()
    cands = actors.others(db, actor)
    if cands:
        Writer(db).append_event(
            source="peer", kind="affordance", ext_id=f"afford-{actor}-{now}",
            ts=now, recv_ts=now, scope=self_of(actor),
            payload=json.dumps({"can_talk_to": cands}, ensure_ascii=False).encode())
    return cands


def observe(db: DB) -> dict:
    """「它们到底认不认识」要能被回答，而不是靠回忆。"""
    first = db.one(
        "SELECT ts, meta FROM outbox WHERE channel='peer' ORDER BY id LIMIT 1")
    pairs = {}
    for r in db.q("SELECT meta, ts FROM outbox WHERE channel='peer'"):
        try:
            m = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            continue
        key = f"{m.get('from')}->{m.get('to')}"
        pairs[key] = pairs.get(key, 0) + 1
    return {
        "first_contact_ts": int(first["ts"]) if first else None,
        "by_pair": pairs,
        "silent": int(db.scalar(
            "SELECT count(*) FROM events WHERE kind='peer_silent'") or 0),
        "affordances": int(db.scalar(
            "SELECT count(*) FROM events WHERE kind='affordance'") or 0),
        "unsaid": int(db.scalar(
            "SELECT count(*) FROM events WHERE kind='unsaid'") or 0),
    }
