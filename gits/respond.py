"""一轮回复：收到消息 → 装配上下文 → 调模型 → 写 outbox。

这是「她回你」的最小闭环。刻意做得窄：

    不抽取     还没写记忆管线，所以 C 段只有最近几轮
    不触发     只回应，不主动开口
    不路由     一律走微信文字

但有三件事是真的：**A/B/C 三段装配、缓存纪律、每次调用都记账**。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gits import ctxwin
from gits.context import (PersonaSeed, RelationSnapshot, TurnContext, assemble,
                          bucket, messages)
from gits.db import DB, Writer, now_ms
from gits.llm import Reply, chat, cost_cny
from gits.recall import lines, recall

DEFAULT_PERSONA = "我不太会哄人。但我记得住你说过的东西。"


@dataclass
class Turn:
    event_id: int
    reply: Reply
    outbox_id: int


def persona_file(path: str | Path) -> Path:
    return Path(path)


def load_persona(path: str | Path | None) -> PersonaSeed:
    """A 段。**一天换一次**就够，所以从文件读；文件变了才换版本号。"""
    if path and Path(path).exists():
        text = Path(path).read_text(encoding="utf-8").strip()
        return PersonaSeed(narrative=text, version=int(
            Path(path).stat().st_mtime))
    return PersonaSeed(narrative=DEFAULT_PERSONA, version=0)


def recent(db: DB, *, from_id: int | None = None, until_id: int | None = None,
           limit: int = 200) -> tuple[tuple[str, str], ...]:
    """**这一扇窗里的全部对话**，不是"最近 6 句"。

    之前写死 6 句，于是 prompt 长度会永远卡在同一个数上——
    60%/80% 两个阈值一辈子都触发不了，窗口也就永远换不掉。
    窗口的边界由 from_id/until_id 定，长度由预算管。
    """
    rows = db.q(
        "SELECT kind, payload FROM events"
        " WHERE kind IN ('msg_in','msg_out')"
        " AND id >= COALESCE(?, 0) AND id <= COALESCE(?, 1<<62)"
        " ORDER BY id DESC LIMIT ?", (from_id, until_id, limit))
    out = []
    for r in reversed(rows):
        who = "他" if r["kind"] == "msg_in" else "我"
        out.append((who, (r["payload"] or b"").decode("utf-8", "replace")))
    return tuple(out)


def relation(db: DB, *, now: int) -> RelationSnapshot:
    """B 段。**一小时内逐字不变**——所以只能放"这个小时都成立"的东西。

    hubs 取最近一再出现的实体；它决定她"顺手会提谁"。现在只有计数，
    等抽取攒够了几百条，这里换成中心度会更有意义。
    """
    n = db.scalar("SELECT count(*) FROM nodes WHERE kind='episode'") or 0
    first = db.scalar("SELECT min(born_ts) FROM nodes WHERE kind='episode'")
    days = max(1, ((now or first or now) - (first or now)) // 86_400_000 + 1)
    hubs = tuple(str(r["content"]) for r in db.q(
        "SELECT c.content FROM nodes n JOIN node_content c ON c.node_id=n.id"
        " WHERE n.kind='entity' ORDER BY n.last_touch DESC LIMIT 3"))
    return RelationSnapshot(
        bucket=bucket(now),
        summary=f"认识第 {days} 天，记下 {n} 件事。" if n else "还没记下什么。",
        hubs=hubs, phase="醒着")


def pending(db: DB, *, limit: int = 3) -> list[dict]:
    """还没被回过、也没被放弃过的 msg_in。"""
    return [dict(r) for r in db.q(
        "SELECT e.* FROM events e WHERE e.kind='msg_in'"
        " AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.ref_event_id=e.id)"
        " ORDER BY e.id LIMIT ?", (limit,))]


def respond(db: DB, event: dict, *, persona: PersonaSeed | None = None,
            model: str = "deepseek-flash", max_tokens: int = 200,
            now: int | None = None) -> Turn:
    """装配 → 调模型 → 落 outbox。**先落库再投递**（§delivery）。"""
    now = now or now_ms()
    text = (event["payload"] or b"").decode("utf-8", "replace")

    # C 段：这一轮才有的东西。**检索在这里发生**——上下文扩展 + r。
    win = ctxwin.current(db)
    history = recent(db, from_id=win["first_event_id"] if win else None,
                     until_id=event["id"])
    seed = persona or load_persona(None)
    snap = relation(db, now=now)
    hits = recall(db, text, context=" ".join(t for _w, t in history),
                  now_ms=now, k=5)
    ctx = TurnContext(stimulus=text, recent=history, recalled=lines(hits))
    prompt = assemble(seed, snap, ctx)
    # 请求体按缓存布局拼：稳定前缀在前，历史轮次只追加，易变的在最后。
    body = messages(seed, snap, ctx, turns=history[:-1] if history else ())
    reply = chat(body, model=model, max_tokens=max_tokens, thinking=False)

    # 记账：窗口长度和命中数都用 API 返回的，不估算（估错了预算就假了）。
    win = win or {"id": ctxwin.open_window(db, ts=now,
                                           first_event_id=event["id"])}
    ctxwin.record(db, win["id"],
                  tokens=int(reply.usage.get("prompt_tokens") or 0),
                  hit_tokens=int(reply.usage.get("prompt_cache_hit_tokens") or 0),
                  event_id=event["id"])

    call_id = Writer(db).append_model_call(
        kind="reply", model=reply.model, response=reply.text,
        event_id=event["id"], usage_json=str(reply.usage),
        prompt_hash=prompt.a_hash,
    )

    contact = db.one("SELECT actor_id, last_context_token FROM contacts"
                     " WHERE channel='weixin' ORDER BY last_seen DESC LIMIT 1")
    with Writer(db).txn() as c:
        cur = c.execute(
            "INSERT INTO outbox(ts, channel, text, status, meta, ref_event_id)"
            " VALUES(?,?,?,'pending',?,?)",
            (now, "weixin", reply.text,
             None if not contact else
             __import__("json").dumps({"to": contact["actor_id"],
                                       "context_token": contact["last_context_token"]},
                                      ensure_ascii=False),
             event["id"]))
        outbox_id = int(cur.lastrowid)

    # 溯源：这句话用了哪版人格、哪次调用（§3.1 可追溯）
    with Writer(db).txn() as c:
        c.execute("INSERT INTO influence(outbox_id, node_id, w, model_call_id)"
                  " VALUES(?,?,?,?)", (outbox_id, f"persona:v{prompt.a_hash[:12]}",
                                       1.0, call_id))
    return Turn(event_id=event["id"], reply=reply, outbox_id=outbox_id)


def respond_pending(db: DB, *, limit: int = 2, **kw) -> list[Turn]:
    return [respond(db, ev, **kw) for ev in pending(db, limit=limit)]
