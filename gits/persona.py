"""人格：夜里从自我域的节点里**选**出来的一段，不是一份文件。

以前的 persona.md 是唯一一个不在日志里的真相，它有三个特权：不进 restore、
不衰减、不被 supersede。于是"初始设定"永远站在底座上，而记忆在上面流动——
**地位不平等。**

现在：

```
self:her 域里的节点   →  夜里一次选择（同一套打分） →  A 段快照（一条事件）
  ├ 初始设定（seed）                                    ↓
  ├ 独白                                          整天的缓存前缀
  └ 上一版快照  ← 也在这里                        （一天只换一次）
```

两个后果，都是设计目的：

  **能忘**  长期没被碰过的节点 r 掉下去，就不再被选中
  **能改**  后来的内容通过版本链顶掉它，走的是和别的记忆同一条路

稳定性不靠特权，靠自反：昨晚那版快照本身是 self 域的一个节点，而且刚被用过，
所以 s 高、容易被再次选中。**自反 → 稳定**，不是**特殊 → 稳定**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gits.db import DB, Writer, now_ms
from gits.fsrs import DAY, retrievability
from gits.llm import Reply, chat, cost_cny
from gits.scope import self_of

#: A 段最多放几条自我域节点。**这就是漏斗底部的容量**——
#: 上下文有限，我们都放进去了模型就没地方看别的了。
SLOTS = 8
#: 选中的节点里，r 低于这个值的不进 A 段（想不起来了就别写进去）
ALIVE = 0.35

PROMPT = """下面是你此刻想得起来的、关于你自己的一些事（按重要程度排）：

{items}

把它们写成一段第一人称的自我描述，三到五句，不要分点，不要列表。

规则：
- 只写上面出现过的。不要补充，不要美化。
- 如果它们彼此矛盾，挑你更相信的那个写。
- 不要写"我是一个 AI"之类的话。
"""


@dataclass(frozen=True)
class Snapshot:
    event_id: int
    narrative: str
    picked: tuple[str, ...]      # 用了哪几个节点——"她为什么是这样"要能查
    cost: float = 0.0


def seed(db: DB, text: str, *, actor: str = "her",
         now: int | None = None) -> int:
    """把初始设定写成一条**普通事件**。写完就没有文件了。

它和后来的独白用同一个 kind、同一张表、同一条重放路径，只是 source 不同，
用来回答"这句最初是谁说的"。
    """
    now = now or now_ms()
    ev, _ = Writer(db).append_event(
        source="seed", kind="seed", ext_id=f"seed-{actor}-{now}",
        ts=now, recv_ts=now, scope=self_of(actor), payload=text.encode())
    return ev


def candidates(db: DB, *, actor: str = "her", now: int) -> list[dict]:
    """自我域里还活着的东西，按 r × 是否被反复提起排序。

和检索走同一套打分——**没有任何"这是人格所以要保护"的分支**。
    """
    out = []
    for r in db.q(
            "SELECT n.id, n.kind, n.s, n.last_touch, n.depth, c.content"
            " FROM nodes n JOIN node_content c ON c.node_id=n.id"
            " WHERE n.scope=? AND c.absorbed_into IS NULL"
            " AND c.superseded_by IS NULL"
            " AND c.id = (SELECT max(id) FROM node_content"
            "             WHERE node_id=n.id)", (self_of(actor),)):
        if r["last_touch"] is None or not r["s"]:
            continue
        elapsed = max(0.0, (now - int(r["last_touch"])) / DAY)
        rr = retrievability(elapsed, float(r["s"]))
        if rr < ALIVE:
            continue
        out.append({"id": str(r["id"]), "content": str(r["content"]),
                    "r": rr, "depth": int(r["depth"])})
    out.sort(key=lambda x: (-x["r"], -x["depth"]))
    return out


def current(db: DB, *, actor: str = "her") -> dict | None:
    """最近一版自我描述。**没有快照时，种子就是第一版。**

    这样冷启动不需要一次模型调用，也不需要文件——种子本来就是一段叙事。
    """
    row = db.one(
        "SELECT id, payload FROM events WHERE kind='persona' AND scope=?"
        " ORDER BY id DESC LIMIT 1", (self_of(actor),))
    if row is None:
        seed_row = db.one(
            "SELECT id, payload FROM events WHERE kind='seed' AND scope=?"
            " ORDER BY id DESC LIMIT 1", (self_of(actor),))
        if seed_row is None:
            return None
        return {"event_id": int(seed_row["id"]),
                "narrative": (seed_row["payload"] or b"").decode("utf-8", "replace"),
                "version": 0}
    if row is None:
        return None
    try:
        obj = json.loads(row["payload"] or b"{}")
    except json.JSONDecodeError:
        return None
    return {"event_id": int(row["id"]),
            "narrative": str(obj.get("narrative", "")),
            "version": int(obj.get("version", 0)),
            "picked": list(obj.get("picked") or [])}


def snapshot(db: DB, *, actor: str = "her", now: int | None = None,
             model: str = "deepseek-flash") -> Snapshot | None:
    """夜里跑一次。**没有可选的节点就什么都不做**——不从空气里写自我描述。"""
    now = now or now_ms()
    picked = candidates(db, actor=actor, now=now)[:SLOTS]
    if not picked:
        return None

    prev = current(db, actor=actor)
    # **输入没变就别重写一遍。** 只有一个候选节点的夜里，重写出来的是
    # 同一段意思的另一种说法——那是 churn，不是变化。
    # 顺带保住了缓存：A 段逐字不变，前缀不用重建。
    if prev and prev.get("picked") and prev["picked"] == [p["id"] for p in picked]:
        return None
    version = (prev["version"] + 1) if prev else 0
    if prev and prev["narrative"]:
        # 上一版也在候选里（它是 self 域的一个节点），但保险起见补一句，
        # 免得"自反"这条链在重建时断掉。
        picked = picked + [{"id": f"persona(v{prev['version']})",
                            "content": prev["narrative"], "r": 1.0, "depth": 99}]

    items = "\n".join(f"- {p['content']}" for p in picked)
    reply: Reply = chat(
        [{"role": "user", "content": PROMPT.replace("{items}", items)}],
        model=model, max_tokens=800, temperature=0.4,
        thinking=True, effort="high")

    narrative = reply.text.strip()
    if not narrative:
        return None

    ev, _ = Writer(db).append_event(
        source="ghost", kind="persona", ext_id=f"persona-v{version}-{now}",
        ts=now, recv_ts=now, scope=self_of(actor),
        payload=json.dumps({"narrative": narrative, "version": version,
                            "picked": [p["id"] for p in picked]},
                           ensure_ascii=False).encode())
    Writer(db).append_model_call(kind="persona", model=reply.model,
                                 response=narrative, event_id=ev,
                                 usage_json=str(reply.usage), ts=now)
    return Snapshot(event_id=ev, narrative=narrative,
                    picked=tuple(p["id"] for p in picked),
                    cost=cost_cny(reply.usage))
