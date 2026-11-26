"""一轮回复：收到消息 → 装配上下文 → 调模型 → 写 outbox。

这是「她回你」的最小闭环。刻意做得窄：

    不抽取     还没写记忆管线，所以 C 段只有最近几轮
    不触发     只回应，不主动开口
    不路由     一律走微信文字

但有三件事是真的：**A/B/C 三段装配、缓存纪律、每次调用都记账**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from gits.speech import ctxwin
from gits.speech.context import (PersonaSeed, RelationSnapshot, TurnContext,
                          bucket, messages, render_a, render_b, render_c)
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
    # **B 段的每一口输入都只许依赖整点，不许依赖 now。**
    #
    # 这句话以前写在文档字符串里，代码没有照做：
    #   hubs 按 last_touch 排序 —— 每碰一次实体，顺序就可能变
    #   affect/style 拿 now 进去 —— 情绪是连续衰减的，相邻两轮可能跨过一个分界
    # 结果就是 system 前缀每轮断一次，而**没有任何东西会报错**——只是命中率
    # 从 90% 掉到 0，账单解释不了。现在统一折到整点。
    t = bucket(now) * 3_600_000
    n = db.scalar("SELECT count(*) FROM nodes WHERE kind='episode'") or 0
    first = db.scalar("SELECT min(born_ts) FROM nodes WHERE kind='episode'")
    days = max(1, ((t or first or t) - (first or t)) // 86_400_000 + 1)
    hubs = tuple(str(r["content"]) for r in db.q(
        "SELECT c.content FROM nodes n JOIN node_content c ON c.node_id=n.id"
        " WHERE n.kind='entity' ORDER BY (n.last_touch / 3600000) DESC, n.id"
        " LIMIT 3"))
    from gits.social.affect import line as affect_line
    from gits.speech.style import line as style_line

    return RelationSnapshot(
        bucket=bucket(now),
        summary=f"认识第 {days} 天，记下 {n} 件事。" if n else "还没记下什么。",
        hubs=hubs, phase="醒着", affect=affect_line(db, actor, now=t),
        style=style_line(db, actor, now=t),
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
        # 标记用**稳定身份**（source + ext_id），不用本地行号：
        # 行号在各库之间不同，副本接管或导入之后对不上——
        # 同一条输入会被当成两条，重答一次。
        " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind='replied'"
        "   AND r.ext_id = 'replied:' || e.source || ':' || e.ext_id)"
        # 旧版写的是 'replied:<行号>'，也认——否则升级之后旧记录全部复活。
        " AND NOT EXISTS (SELECT 1 FROM events r2 WHERE r2.kind='replied'"
        "   AND r2.ext_id = 'replied:' || e.id)"
        " ORDER BY e.id LIMIT ?", (*vis, limit))]


def _prepare(db: DB, *, now: int, actor: str, stimulus: str,
             history: list, persona: PersonaSeed | None = None):
    """装配一轮。**回复和主动开口共用这一条**——
    两套装配并存过，固定事实就是从它们的缝里漏掉的。"""
    seed = persona or load_persona(db)
    snap = relation(db, now=now, actor=actor)
    wide = recall(db, stimulus, actor=actor,
                  context=" ".join(t for _w, t in history),
                  now_ms=now, k=SLOTS * 3)
    from gits.memory.spectrum import allocate
    from gits.social.affect import distribution as affect_dist

    mood = affect_dist(db, actor, now=now)
    hits = allocate(db, wide, slots=SLOTS, arousal=mood["arousal"])
    from gits.speech.facts import lines as fact_lines
    from gits.world import forecast as fc

    due = tuple(fact_lines(db, now)) + tuple(fc.lines(db, now))
    from gits.world import brief_text

    fresh_window = not history
    # **角色事实和解读规则一样：只在上下文开头出现一次。**
    # 它是她自己的事，不会一小时一变——每轮重复只是白烧 token。
    from gits.memory import character

    self_facts = character.line(db, now=now) if fresh_window else ""
    # 关系账本和角色事实同一块：**数字，不是句子**。空账不出现在上下文里。
    from gits.memory import relations

    if fresh_window:
        rel = relations.brief(db, now=now)
        if rel:
            self_facts = (self_facts + chr(10) if self_facts else "") + rel

    background = brief_text(db, now=now, actor=actor, head=fresh_window)
    if self_facts:
        background = ("# 关于你自己（事实，不是设定）" + chr(10)
                      + self_facts + chr(10) + background).strip()
    ctx = TurnContext(stimulus=stimulus, recent=tuple(history), recalled=lines(hits),
                      due=due, background=background)
    a_hash = render_a(seed)
    body = messages(seed, snap, ctx, turns=history[:-1] if history else ())
    return seed, snap, ctx, a_hash, body, hits


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
    seed, snap, ctx, a_hash, body, hits = _prepare(
        db, now=now, actor=actor, stimulus=text, history=history,
        persona=persona)

    # **工具按节奏放行。** 高频对话里多一个来回就是"卡住了"——
    # pacing 判间隔分布，主动开口永远允许。目录为空时整条路径不生效。
    from gits.speech import pacing, tools_loop
    from gits.world import catalog, detail

    # 主动开口：没有人等着，等一下不突兀。由调用方（ghost）显式标注，
    # 不靠 payload 里的魔法前缀去猜。
    proactive = bool(event.get("proactive"))
    allowed, _why = pacing.tools_allowed(db, now=now, proactive=proactive)
    cat = catalog(db, now=now, proactive=proactive) if allowed else ()
    reply = tools_loop.run(
        chat, body, catalog=cat,
        detail=lambda kind, **kw: detail(db, kind, now=now, **kw),
        now=now, model=model, max_tokens=max_tokens, thinking=False)

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
                  # **缺字段就是不知道**，不是 0 命中——两者混在一起会让
                  # "provider 不报命中"看起来像"缓存坏了"，然后一直换窗付全价。
                  hit_tokens=(int(reply.usage["prompt_cache_hit_tokens"])
                              if reply.usage.get("prompt_cache_hit_tokens")
                              is not None else None),
                  event_id=event["id"])

    # **记下这一轮的形状。** 命中率低的时候，第一个要回答的问题是
    # "A/B/C 哪一段在动"——没有这个数，只能猜。只存指纹，正文是内容。
    from gits.speech import shape

    shape.record(db, now=now, a=render_a(seed), b=render_b(snap),
                 c=render_c(ctx), window_id=win["id"])

    # **记账。**回复照记，但**不拦**——"配额不该表现为她坏了"（DESIGN §18.6）。
    # 拦只拦主动开口：那是她自己的开销，说完没人理也照样花。
    from gits.io import ledger as _ledger

    _ledger.charge_usage(db, actor=actor, usage=reply.usage, now=now,
                         what="reply", ref=f"reply:{event['id']}")

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
        source="ghost", kind="replied",
        ext_id=f"replied:{event['source']}:{event['ext_id']}",
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
