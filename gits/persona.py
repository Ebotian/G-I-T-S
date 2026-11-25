"""人格：一版一版地长，不回退。

三条规矩，都是为了防两种退化——一个怎么都不变（死），一个每句话都变（没有自我）：

  **必须能给依据**  每处改动挂 event_id。挂不上 = 整次作废。
  **变化必须有界**  一次最多改 60 个字。改多了不叫变化，叫换人。
  **允许不变**      `keep: true` 应该是最常见的输出。

为什么**不做回退**：需要回退，说明我们不信任写入机制；有按钮就敢乱写。
不回退 ≠ 不记录——版本链是 append-only 的，「她为什么变成这样」一年后还得答得上。
这跟 `redactions`（把不该存在的话撤掉）是两件事，别混。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gits.context import PersonaSeed
from gits.db import DB, Writer, now_ms
from gits.llm import Reply, chat, cost_cny
from gits.scope import self_of

#: 一次最多改多少字。超了就当模型在重写。
MAX_CHARS = 60
#: 一晚至少要看到这么多条新事件才值得更新
MIN_EVIDENCE = 3

PROMPT = """这是你现在的自我描述：

{narrative}

下面是最近发生过的事（每条前面是事件编号）：

{evidence}

如果这些事真的改变了你看待自己或对待他的方式，就改一两句；
如果没有，就原样保留。**大部分日子不该有变化。**

只输出 JSON：
{{"keep": true/false,
 "changes": [{{"old": "原句逐字", "new": "新句", "because": [事件编号]}}]}}

规则：
- old 必须是上面那段自我描述里**逐字存在**的一句。
- 一次最多改 {max_chars} 个字。
- 没有真正的变化就 keep=true、changes 留空。这很正常。
"""


@dataclass(frozen=True)
class Version:
    event_id: int
    narrative: str
    version: int


@dataclass
class Changed:
    event_id: int
    keep: bool
    changes: int
    narrative: str
    cost: float


def latest(db: DB, *, actor: str = "her") -> Version | None:
    row = db.one(
        "SELECT id, payload FROM events WHERE kind='persona' AND scope=?"
        " ORDER BY id DESC LIMIT 1", (self_of(actor),))
    if row is None:
        return None
    try:
        obj = json.loads(row["payload"] or b"{}")
    except json.JSONDecodeError:
        return None
    return Version(event_id=int(row["id"]),
                   narrative=str(obj.get("narrative", "")),
                   version=int(obj.get("version", 0)))


def seed(db: DB, *, actor: str = "her", fallback: str = "") -> PersonaSeed:
    v = latest(db, actor=actor)
    if v is None:
        return PersonaSeed(narrative=fallback, version=0)
    return PersonaSeed(narrative=v.narrative, version=v.version)


def evidence(db: DB, *, actor: str, since_id: int, limit: int = 40) -> list[tuple[int, str]]:
    from gits.scope import visible

    vis = visible(actor)
    rows = db.q(
        "SELECT id, payload FROM events WHERE kind IN ('msg_in','msg_out','inner')"
        " AND scope IN (%s) AND id > ? ORDER BY id DESC LIMIT ?"
        % ",".join("?" * len(vis)), (*vis, since_id, limit))
    return [(int(r["id"]), (r["payload"] or b"").decode("utf-8", "replace"))
            for r in reversed(rows)]


def validate(obj: dict, *, base: str, known: set[int]) -> list[dict]:
    """三道门。任何一道不过，就整次作废——不是"改小一点再试"。"""
    changes = obj.get("changes") or []
    if not changes:
        return []
    if sum(len(str(c.get("new", ""))) - len(str(c.get("old", "")))
           for c in changes) > MAX_CHARS:
        raise ValueError(f"一次改太多（上限 {MAX_CHARS} 字），这是在重写")
    out = []
    for c in changes:
        old, new = str(c.get("old", "")), str(c.get("new", ""))
        if not old or old not in base:
            raise ValueError(f"old 不是原文里逐字存在的一句：{old[:20]!r}")
        refs = [int(x) for x in (c.get("because") or []) if str(x).isdigit()]
        if not refs:
            raise ValueError("改动没有依据（because 是空的）")
        missing = [r for r in refs if r not in known]
        if missing:
            raise ValueError(f"依据的事件不存在：{missing}")
        out.append({"old": old, "new": new, "because": refs})
    return out


def update(db: DB, *, actor: str = "her", now: int | None = None,
           fallback: str = "", model: str = "deepseek-flash") -> Changed | None:
    """夜里跑一次。**没有新经历就不调用模型**——从空气里写自我描述是种病。"""
    now = now or now_ms()
    cur = latest(db, actor=actor)
    base = cur.narrative if cur else fallback
    since = cur.event_id if cur else 0
    ev = evidence(db, actor=actor, since_id=since)
    if len(ev) < MIN_EVIDENCE:
        return None

    text = "\n".join(f"[{i}] {t}" for i, t in ev)
    reply: Reply = chat(
        [{"role": "user", "content": PROMPT.format(
            narrative=base, evidence=text, max_chars=MAX_CHARS)}],
        model=model, max_tokens=1500, temperature=0.2,
        thinking=True, effort="high")            # 后台活，该想多深想多深

    raw = reply.text
    try:
        obj = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        changes = validate(obj, base=base, known={i for i, _ in ev})
    except (ValueError, json.JSONDecodeError) as exc:
        Writer(db).append_model_call(kind="persona_failed", model=reply.model,
                                     response=raw, usage_json=str(reply.usage), ts=now)
        raise ValueError(f"{exc}；原始输出已留档") from exc

    narrative = base
    for c in changes:
        narrative = narrative.replace(c["old"], c["new"], 1)
    version = (cur.version if cur else 0) + (1 if changes else 0)

    ev_id, _ = Writer(db).append_event(
        source="ghost", kind="persona", ext_id=f"persona-v{version}-{now}",
        ts=now, recv_ts=now, scope=self_of(actor),
        payload=json.dumps({"narrative": narrative, "version": version,
                            "changes": changes, "keep": not changes},
                           ensure_ascii=False).encode())
    Writer(db).append_model_call(kind="persona", model=reply.model, response=raw,
                                 event_id=ev_id, usage_json=str(reply.usage), ts=now)
    return Changed(event_id=ev_id, keep=not changes, changes=len(changes),
                   narrative=narrative, cost=cost_cny(reply.usage))
