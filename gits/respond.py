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

from gits.context import PersonaSeed, RelationSnapshot, TurnContext, assemble, bucket
from gits.db import DB, Writer, now_ms
from gits.llm import Reply, chat, cost_cny

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


def recent(db: DB, n: int = 6) -> tuple[tuple[str, str], ...]:
    rows = db.q(
        "SELECT kind, payload FROM events"
        " WHERE kind IN ('msg_in','msg_out') ORDER BY id DESC LIMIT ?", (n,))
    out = []
    for r in reversed(rows):
        who = "他" if r["kind"] == "msg_in" else "我"
        out.append((who, (r["payload"] or b"").decode("utf-8", "replace")))
    return tuple(out)


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

    prompt = assemble(
        persona or load_persona(None),
        RelationSnapshot(bucket=bucket(now), summary="（还没有关系状态）",
                         hubs=(), phase="醒着"),
        TurnContext(stimulus=text, recent=recent(db), recalled=()),
    )
    reply = chat(
        [{"role": "system", "content": prompt.text},
         {"role": "user", "content": text}],
        model=model, max_tokens=max_tokens, thinking=False,
    )

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
