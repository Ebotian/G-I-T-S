"""投递：outbox 的状态机。

    pending ──claim(租约)──► pending(已租) ──成功──► sent
                                       └──失败──► pending(重试) ──次数用尽──► failed
    超过 TTL 还没发出去 ──► dropped(expired)

三条铁律：

  1. **先落库再发**。发出去之后才记状态，中间崩了就是"结果未知"——
     这正是 §17.4 那三个崩溃场景里的第二个。所以状态里必须有 `unknown`
     这个能长期停留的取值，不能自动重试成 sent。
  2. **租约防重复**。两个 shell 同时跑，不能把同一条发两次。
  3. **重试有上限**。无限重试等于无限骚扰。
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.db import DB, Writer, now_ms


@dataclass
class Dispatch:
    sent: int = 0
    failed: int = 0
    expired: int = 0
    retried: int = 0

    def __bool__(self) -> bool:
        return any((self.sent, self.failed, self.expired, self.retried))

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in
                 (("sent", self.sent), ("failed", self.failed),
                  ("expired", self.expired), ("retried", self.retried)) if v]
        return " ".join(parts) or "无"


def claim(db: DB, *, now: int | None = None, limit: int = 20,
          lease_ms: int = 60_000) -> list[dict]:
    """把 pending 领到手。带租约，别人这段时间抢不走。"""
    now = now or now_ms()
    rows = db.q(
        "SELECT * FROM outbox WHERE status='pending'"
        " AND (lease_until IS NULL OR lease_until < ?)"
        " ORDER BY id LIMIT ?", (now, limit))
    if not rows:
        return []
    with Writer(db).txn() as c:
        for r in rows:
            c.execute("UPDATE outbox SET lease_until=?, attempts=attempts+1"
                      " WHERE id=?", (now + lease_ms, r["id"]))
    # 重新读一遍：调用方看到的 attempts 必须是**已经加过**的那个，
    # 否则第一次失败时 max_attempts 的判断会差一。
    ids = ",".join("?" * len(rows))
    return [dict(r) for r in db.q(
        f"SELECT * FROM outbox WHERE id IN ({ids}) ORDER BY id",
        tuple(r["id"] for r in rows))]


def finish(db: DB, outbox_id: int, status: str, *, meta: str = "") -> None:
    with Writer(db).txn() as c:
        c.execute("UPDATE outbox SET status=?, meta=?, lease_until=NULL WHERE id=?",
                  (status, meta, outbox_id))


def expire(db: DB, *, now: int | None = None, ttl_ms: int = 10 * 60_000) -> int:
    """超过 TTL 还在 pending 的，如实记 dropped。**不许无限挂着。**"""
    now = now or now_ms()
    rows = db.q("SELECT id FROM outbox WHERE status='pending' AND ts < ?",
                (now - ttl_ms,))
    for r in rows:
        finish(db, r["id"], "dropped", meta="expired")
    return len(rows)


def dispatch(db: DB, channel, *, now: int | None = None, max_attempts: int = 3,
             ttl_ms: int = 10 * 60_000, limit: int = 20) -> Dispatch:
    """领一批、发一批、记一批。"""
    now = now or now_ms()
    out = Dispatch()
    out.expired = expire(db, now=now, ttl_ms=ttl_ms)

    for row in claim(db, now=now, limit=limit):
        if row["attempts"] and int(row["attempts"]) > max_attempts:
            finish(db, row["id"], "failed", meta="too-many-attempts")
            out.failed += 1
            continue
        try:
            status, meta = channel.deliver(row)
        except Exception as exc:                      # 通道炸了不是她的错
            if int(row.get("attempts") or 0) >= max_attempts:
                finish(db, row["id"], "failed", meta=f"err:{type(exc).__name__}")
                out.failed += 1
            else:
                with Writer(db).txn() as c:
                    c.execute("UPDATE outbox SET lease_until=NULL WHERE id=?",
                              (row["id"],))
                out.retried += 1
            continue

        finish(db, row["id"], status, meta=meta)
        if status == "sent":
            out.sent += 1
            with Writer(db).txn() as c:
                c.execute("INSERT INTO outcomes(outbox_id, ts, kind, ref_event_id)"
                          " VALUES(?,?,?,?)", (row["id"], now, "delivered", None))
        elif status == "failed":
            out.failed += 1
    return out
