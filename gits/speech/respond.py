"""一轮回复：收到消息 → 装配上下文 → 调模型 → 写 outbox。

这是「她回你」的最小闭环。刻意做得窄：

    不抽取     还没写记忆管线，所以 C 段只有最近几轮
    不触发     只回应，不主动开口
    不路由     一律走微信文字

但有三件事是真的：**A/B/C 三段装配、缓存纪律、每次调用都记账**。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from gits.speech import ctxwin
from gits.speech.context import (PersonaSeed, RelationSnapshot, TurnContext,
                          bucket, messages, render_a)
from gits.core.db import DB, Writer, now_ms
from gits.speech.llm import Reply, chat, cost_cny
from gits.memory.recall import lines, recall, reinforce_hits
from gits.core.scope import visible

DEFAULT_PERSONA = "我不太会哄人。但我记得住你说过的东西。"


@dataclass
class Turn:
    event_id: int
    reply: Reply
    outbox_id: int


def persona_file(path: str | Path) -> Path:
    return Path(path)


class NoPersona(RuntimeError):
    """还没有任何自我描述。**这时候不该说话。**"""


def load_persona(db: DB | None = None) -> PersonaSeed:
    """A 段：最近一次夜里"选"出来的那版快照。

    **不读文件。**初始设定也是事件（seed），和后来的独白同一个 kind、
    同一条重放路径——所以它会衰减、会被顶掉，地位和别人一样。
    """
    if db is None:
        raise NoPersona("load_persona 需要 db：人格现在存在事件里，不是文件里")
    from gits.memory.persona import current

    cur = current(db)
    if cur is None or not cur["narrative"]:
        raise NoPersona(
            "还没有自我描述。先导入种子：gits seed \"……\"，"
            "或者等夜里跑一次 persona.snapshot")
    return PersonaSeed(narrative=cur["narrative"], version=cur["version"])


def recent(db: DB, *, actor: str = "her", from_id: int | None = None,
           until_id: int | None = None,
           limit: int = 200) -> tuple[tuple[str, str], ...]:
    """**这一扇窗里的全部对话**，不是"最近 6 句"。

    之前写死 6 句，于是 prompt 长度会永远卡在同一个数上——
    60%/80% 两个阈值一辈子都触发不了，窗口也就永远换不掉。
    窗口的边界由 from_id/until_id 定，长度由预算管。
    """
    vis = visible(actor)
    rows = db.q(
        "SELECT kind, payload FROM events"
        " WHERE kind IN ('msg_in','msg_out')"
        " AND scope IN (%s)" % ",".join("?" * len(vis)) +
        " AND id >= COALESCE(?, 0) AND id <= COALESCE(?, 1<<62)"
        " ORDER BY id DESC LIMIT ?", (*vis, from_id, until_id, limit))
    out = []
    for r in reversed(rows):
        who = "他" if r["kind"] == "msg_in" else "我"
        out.append((who, (r["payload"] or b"").decode("utf-8", "replace")))
    return tuple(out)


#: C 段里放几条记忆。频带分配的上限就是这个数。
SLOTS = 5
#: 一条微信最多多少字（清洗时的硬帽）
REPLY_CAP_CHARS = 400


def relation(db: DB, *, now: int, actor: str = "her") -> RelationSnapshot:
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
    from gits.social.affect import line as affect_line
    from gits.speech.style import line as style_line


    return RelationSnapshot(
        bucket=bucket(now),
        summary=f"认识第 {days} 天，记下 {n} 件事。" if n else "还没记下什么。",
        hubs=hubs, phase="醒着", affect=affect_line(db, actor, now=now),
        style=style_line(db, actor, now=now),
    )


def pending(db: DB, *, limit: int = 3, actor: str = "her") -> list[dict]:
    """**这一轮该她回的**消息。

    两个条件缺一不可，各自对应一次被实测到的问题：

      ① 可见域内（scope）
         以前不筛，于是 peer 发给 peer 的私信（scope=self:peer2）会被主实例领走，
         当作「他对我说的话」——正文进模型、回复发到微信。

      ② 没有 replied 标记
         以前靠 outbox.ref_event_id 判断回没回过，而 outbox 是投影表，
         project.restore() 会整表清空 → 夜里 replay 之后所有旧消息重新变成
         待回复：重答、重发、重复付费。**标记必须落成事件**才经得起重放。
    """
    from gits.core.scope import visible

    vis = visible(actor)
    marks = ",".join("?" * len(vis))
    return [dict(r) for r in db.q(
        "SELECT e.* FROM events e WHERE e.kind='msg_in'"
        " AND e.scope IN (" + marks + ")"
        " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind='replied'"
        "   AND r.ext_id = 'replied:' || e.id)"
        " ORDER BY e.id LIMIT ?", (*vis, limit))]


def respond(db: DB, event: dict, *, persona: PersonaSeed | None = None,
            model: str = "deepseek-flash", max_tokens: int = 200,
            actor: str = "her", now: int | None = None) -> Turn:
    """装配 → 调模型 → 落 outbox。**先落库再投递**（§delivery）。"""
    now = now or now_ms()
    text = (event["payload"] or b"").decode("utf-8", "replace")

    # C 段：这一轮才有的东西。**检索在这里发生**——上下文扩展 + r。
    win = ctxwin.current(db)
    history = recent(db, actor=actor,
                     from_id=win["first_event_id"] if win else None,
                     until_id=event["id"])
    seed = persona or load_persona(db)
    snap = relation(db, now=now, actor=actor)
    # 先宽召回，再按频谱分配——直接把 k 当条数会让同一个频带被填满。
    wide = recall(db, text, actor=actor,
                  context=" ".join(t for _w, t in history),
                  now_ms=now, k=SLOTS * 3)
    from gits.memory.spectrum import allocate
    from gits.social.affect import distribution as affect_dist

    mood = affect_dist(db, actor, now=now)
    hits = allocate(db, wide, slots=SLOTS, arousal=mood["arousal"])
    from gits.speech.facts import lines as fact_lines
    from gits.world import forecast as fc

    # C 段的日子：生日这类记录 + 窗口内的预报。**两类都是记录，不是读数。**
    due = tuple(fact_lines(db, now)) + tuple(fc.lines(db, now))
    # 背景**每轮追加**在易变区开头。装配不知道它从哪来——
    # 它只认 gits.world.brief_text() 这一个入口。
    from gits.world import brief_text

    ctx = TurnContext(stimulus=text, recent=history, recalled=lines(hits),
                      due=due, background=brief_text(db, now=now, actor=actor))
    # 只要 A 段的哈希（用于 influence 溯源），不必再跑一遍完整装配——
    # 两套装配并存过一段时间，固定事实就是从它们的缝里漏掉的。
    a_hash = render_a(seed)
    # 请求体按缓存布局拼：稳定前缀在前，历史轮次只追加，易变的在最后。
    body = messages(seed, snap, ctx, turns=history[:-1] if history else ())
    reply = chat(body, model=model, max_tokens=max_tokens, thinking=False)

    # **出口清洗**：整句收笔、洗链接、去交差腔。
    # max_tokens 是保险丝不是尺——顶头会拦腰掐断，一条微信停在逗号上比不发更糟。
    from gits.speech.clean import clean

    reply.text = clean(reply.text, cap=REPLY_CAP_CHARS)

    # **成功之后才强化**（spacing effect）。放在模型调用之前的话，
    # 一次失败的请求也会把 s 顶上去——那条记忆其实没被用上。
    # 强化落成事件，所以夜里的 replay --restore 不会把它抹掉。
    reinforce_hits(db, hits, now_ms=now)

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
        prompt_hash=hashlib.sha256(a_hash.encode()).hexdigest(),
    )

    # **收件人由触发这条回复的消息决定**，不是"最近联系人"。
    # 只有一个用户时看不出差别；但两颗消息交错处理时，后者会把前者的
    # 回复发到另一个人那里——而这种 bug 要等到有第二个人那天才现形。
    route = db.one("SELECT channel, actor_id, context_token FROM event_route"
                   " WHERE event_id=?", (event["id"],))
    contact = route
    if contact is None:                     # 老事件（v13 之前）退回旧办法
        contact = db.one("SELECT actor_id, last_context_token FROM contacts"
                         " WHERE channel='weixin' ORDER BY last_seen DESC LIMIT 1")
    channel = (contact["channel"] if contact and "channel" in contact.keys()
               else "weixin")
    token = (contact["context_token"] if contact and "context_token" in contact.keys()
             else (contact["last_context_token"] if contact else None))
    with Writer(db).txn() as c:
        cur = c.execute(
            "INSERT INTO outbox(ts, channel, text, status, meta, ref_event_id)"
            " VALUES(?,?,?,'pending',?,?)",
            (now, channel, reply.text,
             None if not contact else
             __import__("json").dumps({"to": contact["actor_id"],
                                       "context_token": token},
                                      ensure_ascii=False),
             event["id"]))
        outbox_id = int(cur.lastrowid)

    # **回过了**——落成事件，不是靠 outbox 那行。重放会清空的只有投影。
    Writer(db).append_event(
        source="ghost", kind="replied", ext_id=f"replied:{event['id']}",
        ts=now, recv_ts=now, scope=event["scope"], payload=b"")

    # 溯源：这句话用了哪版人格、哪次调用（§3.1 可追溯）
    with Writer(db).txn() as c:
        persona_tag = "persona:v" + hashlib.sha256(
            a_hash.encode()).hexdigest()[:12]
        c.execute("INSERT INTO influence(outbox_id, node_id, w, model_call_id)"
                  " VALUES(?,?,?,?)", (outbox_id, persona_tag, 1.0, call_id))
        # **把真正进了上下文的节点也记下来**。
        # 只记人格哈希的话，事后能回答"用了哪版人格"，却回答不了
        # "这句话凭什么"——而后者才是她能被追问的那个问题。
        for rank, h in enumerate(hits):
            c.execute("INSERT INTO influence(outbox_id, node_id, w, model_call_id)"
                      " VALUES(?,?,?,?)", (outbox_id, h.node_id,
                                           round(float(h.score), 6), call_id))
    return Turn(event_id=event["id"], reply=reply, outbox_id=outbox_id)


def respond_pending(db: DB, *, limit: int = 2, **kw) -> list[Turn]:
    return [respond(db, ev, **kw) for ev in pending(db, limit=limit)]
