"""整理：关掉一扇窗，把它写成一段记忆。

和普通聊天机器人最大的区别在这里——**整理不是丢弃上下文，是把它变成事件**：

    关窗 = 把窗里的对话压成事实 → events(kind='consolidate') → 图上的节点

所以整理出来的东西：

  - 可重放（它就是事件，`project.restore` 认它）
  - 可检索（走同一条 recall 路径，跟别的记忆一视同仁）
  - 可审计（谁在什么时候改了自我叙事，日志里有）

窗口换掉之后，A/B 段按新的图重写——她"忘了细节，但记得发生过什么"。
人也是这样：你不记得三天前那顿饭的每句话，你记得那顿饭让你不舒服。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gits.speech import ctxwin
from gits.core.db import DB, Writer, now_ms
from gits.speech.llm import Reply, chat, cost_cny

PROMPT = """下面是刚刚聊过的一段。把它整理成她的记忆，只输出 JSON。

{{"summary": "这段发生了什么，两三句，第三人称",
 "facts": ["值得长期记住的事实，一条一句，最多 5 条"],
 "unfinished": ["还没说完的事，没有就空数组"]}}

规则：
- 写发生了什么，不写你的感受，不写建议。
- 具体的人、事、时间点要留住；寒暄不要留。
- 只写这段里真的出现过的。
- 不要写情绪判断。情绪由 affect.py 从每条消息单独估、单独衰减，
  不从这里来——一段对话的"总体气氛"会把噪声平均成结论。

对话：
{dialogue}"""


@dataclass
class Consolidated:
    event_id: int
    window_id: int
    summary: str
    facts: int
    latency_ms: int
    cost: float


def dialogue_of(db: DB, win: dict, *, limit: int = 80) -> str:
    rows = db.q(
        "SELECT kind, payload FROM events WHERE kind IN ('msg_in','msg_out')"
        " AND id >= COALESCE(?, 0) AND id <= COALESCE(?, 1<<62)"
        " ORDER BY id LIMIT ?",
        (win.get("first_event_id"), win.get("last_event_id"), limit))
    out = []
    for r in rows:
        who = "他" if r["kind"] == "msg_in" else "她"
        out.append(f"{who}：{(r['payload'] or b'').decode('utf-8', 'replace')}")
    return "\n".join(out)


def compact(db: DB, *, now: int | None = None, model: str = "deepseek-flash",
            reason: str = "手动") -> Consolidated | None:
    """关掉当前窗口并写一条 consolidate 事件。窗口里没东西就什么都不做。"""
    now = now or now_ms()
    win = ctxwin.current(db)
    if win is None:
        return None
    text = dialogue_of(db, win)
    if not text.strip():
        ctxwin.close(db, win["id"], reason="空窗口", ts=now)
        return None

    reply: Reply = chat(
        [{"role": "user", "content": PROMPT.replace("{dialogue}", text)}],
        model=model, max_tokens=1500, temperature=0.2,
        thinking=True, effort="high")          # 后台活，该想多深想多深

    try:
        obj = json.loads(reply.text[reply.text.index("{"): reply.text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        obj = {"summary": reply.text.strip()[:500], "facts": [], "unfinished": []}

    # 整理结果**本身就是事件**：重放时不需要再调一次模型。
    ev, _ = Writer(db).append_event(
        source="ghost", kind="consolidate", ext_id=f"w{win['id']}",
        ts=now, recv_ts=now,
        payload=json.dumps(obj, ensure_ascii=False).encode())
    Writer(db).append_model_call(
        kind="consolidate", model=reply.model, response=reply.text,
        event_id=ev, usage_json=str(reply.usage), ts=now)

    ctxwin.close(db, win["id"], reason=reason, ts=now)
    new_id = ctxwin.open_window(db, ts=now, first_event_id=ev)
    return Consolidated(event_id=ev, window_id=new_id,
                        summary=str(obj.get("summary", ""))[:200],
                        facts=len(obj.get("facts") or []),
                        latency_ms=reply.latency_ms, cost=cost_cny(reply.usage))

