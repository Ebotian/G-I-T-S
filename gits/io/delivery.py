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

from gits.core.db import DB, Writer, now_ms


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


#: 一条 pending 超过这么久还没发出去，就当通道堵了。
#: 取 30 分钟：expire 的 TTL 是 10 分钟（那是「这条彻底发不出去了」），
#: 这里是「现在别再生新的」——两件事，两个数。
STALE_MS = 30 * 60_000


def channel_ready(db: DB, *, now: int | None = None,
                  stale_ms: int = STALE_MS) -> tuple[bool, str]:
    """通道现在能不能用。**返回理由，不只是布尔。**

    判据是**结果**，不是心跳：有没有联系人、有没有攒着发不出去的东西。
    没有主见的时候一律放行——「不知道」不等于「断了」，和互惠系数一个口径。

    这以前是空的：gate 里有一道 if not i.channel_online，但没有任何调用方
    传过这个参数，默认恒为 True。微信挂掉的时候她照样一次次决定要开口、
    一次次调模型。
    """
    now = now if now is not None else now_ms()
    row = db.one("SELECT actor_id FROM contacts LIMIT 1")
    if row is None:
        return (False, "通道不认识收件人")
    stuck = db.one(
        "SELECT ts FROM outbox WHERE status='pending'"
        " AND ts < ? ORDER BY ts LIMIT 1", (now - stale_ms,))
    if stuck is not None:
        mins = (now - int(stuck["ts"])) // 60_000
        return (False, f"积压堵了 {mins} 分钟，先别再生新的")
    return (True, "")


def claim(db: DB, *, now: int | None = None, limit: int = 20,
          lease_ms: int = 60_000,
          names: set[str] | frozenset[str] | None = None) -> list[dict]:
    """把 pending 领到手。带租约，别人这段时间抢不走。

    `names` 限定这个通道认得哪些名字——否则文件通道会把微信的活儿也领走，
    然后"发到文件里"，而你以为发出去了。
    """
    now = now or now_ms()
    sql = ("SELECT * FROM outbox WHERE status='pending'"
           " AND (lease_until IS NULL OR lease_until < ?)")
    args: list = [now]
    if names:
        sql += " AND channel IN (%s)" % ",".join("?" * len(names))
        args.extend(sorted(names))
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    rows = db.q(sql, args)
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

    for row in claim(db, now=now, limit=limit,
                     names=getattr(channel, "names", None)):
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
            # **发出去了才算说过。** 不写这条，下一轮装配里只有他的消息、
            # 没有她自己的话——她会"忘记自己刚说过什么"。
            Writer(db).append_event(
                source=getattr(channel, "name", "channel"), kind="msg_out",
                ext_id=f"outbox:{row['id']}", ts=now, recv_ts=now,
                payload=str(row["text"]).encode())
        elif status == "failed":
            out.failed += 1
    return out
