"""主动开口的执行路径：**从"她决定要说"到"通道接了"。**

以前这条是断的：`ghost.initiative()` 判定放行后只落一条 initiative 事件就返回，
库里有"决定"、没有"待发消息"，而 `spoken()` 又把它计为说过——
压力归零，之后还会为一句他没收到的话累计"没回"的惩罚。

现在把四个时刻分开：

    她想起了      候选（unresolved.sample）
    她决定说      initiative 事件        ← 只是内心事件
    通道已接收    outbox 行             ← 从这里才叫"说过"
    用户回应了    他的 msg_in（world 域）← 只有这个算回应

这一步只做第 2 到第 3：把决定变成一条待发消息。
发不出去（通道离线）就什么都不写——决定留在日志里，压力照旧攒着。
"""

from __future__ import annotations

import json

from gits.core.db import DB, Writer, now_ms


def stimulus(candidate: str | None, *, reason: str = "") -> str:
    """主动开口的刺激源。**给意图和名词，不写句子。**

    写成一整句（"你想问问他看病的事"）就是脚本化措辞，
    而她该决定怎么说——和我们把背景块改成数据而不是句子是同一条原则。
    """
    if candidate:
        return f"（你心里有件事没放下：{candidate}）"
    return "（你想说点什么）"


def _entity_of(name: str) -> str:
    from gits.core.project import entity_id

    return entity_id(name)


def compose(db: DB, *, initiative_event: int, candidate: str | None,
            channel: str, to: dict | None = None, actor: str = "her",
            model: str = "deepseek-flash", max_tokens: int = 200,
            now: int | None = None) -> int | None:
    """生成并**入队**一条主动消息。返回 outbox id。

    `initiative_event` 会被写进 outbox 的 ref_event_id——
    这样 `spoken()` 才能知道"这次决定真的走出去了"，`trace` 也追得到由来。
    """
    from gits.speech.respond import _prepare
    from gits.speech.tools_loop import run as run_tools
    from gits.speech.llm import chat
    from gits.speech import pacing
    from gits.world import catalog, detail

    now = now or now_ms()
    text = stimulus(candidate)
    _seed, _snap, _ctx, _h, body, _hits = _prepare(
        db, now=now, actor=actor, stimulus=text, history=[])

    cat = catalog(db, now=now, proactive=True)
    reply = run_tools(
        chat, body, catalog=cat,
        detail=lambda kind, **kw: detail(db, kind, now=now, proactive=True, **kw),
        now=now, model=model, max_tokens=max_tokens, thinking=False)

    from gits.speech.clean import clean

    body_text = clean(reply.text, cap=400)
    if not body_text:
        return None                      # 什么都没说出来，不算说过

    with Writer(db).txn() as c:
        cur = c.execute(
            "INSERT INTO outbox(ts, channel, text, status, meta, ref_event_id)"
            " VALUES(?,?,?,'pending',?,?)",
            (now, channel, body_text,
             json.dumps(to, ensure_ascii=False) if to else None,
             initiative_event))
        outbox_id = int(cur.lastrowid)

    # **反刍计数：真的围绕同一件事开过口，才记一次。**
    # 以前只有测试调 record_use()，正常路径从不记——
    # "同一件事问过多次就翻篇"这个机制在线上从来没生效过。
    if candidate:
        from gits.speech import unresolved

        known = int(db.scalar(
            "SELECT count(*) FROM edges WHERE dst=? AND kind='claims'",
            (__import__("gits.core.project", fromlist=["entity_id"])
             .entity_id(candidate),)) or 0)
        unresolved.record_use(db, _entity_of(candidate), known=known, now=now)

    # 记账挂在 initiative 事件上——这一轮的 token 是这次决定的成本。
    Writer(db).append_model_call(
        kind="opening", model=reply.model, event_id=initiative_event,
        response=body_text, usage_json=str(reply.usage))
    return outbox_id
