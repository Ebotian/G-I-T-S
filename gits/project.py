"""投影：从日志集再生出 nodes / edges / outbox / influence。

关键约束（DESIGN §3.1）：

    恢复重放 **只读已持久化的模型结果**，绝不重新调用模型。

所以 :func:`restore` 拿不到 `model_calls` 时会**直接失败**，
而不是"顺手再问一次"——那就不叫恢复了。

投影函数必须是纯的：给定同一份日志集，输出逐字节相同。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .db import DB, PROJECTION_TABLES, Writer


class MissingModelResult(RuntimeError):
    """恢复重放时找不到已保存的模型结果。这是硬错误，不是可以兜底的警告。"""


def node_id_for(source: str, ext_id: str) -> str:
    """确定性 id：同一件事永远生成同一个节点。

    这样重放不需要"记住上次生成的 id"，也就不需要 id 映射表。
    """
    h = hashlib.sha256(f"{source}\x00{ext_id}".encode()).hexdigest()[:16]
    return f"n:{h}"


@dataclass(frozen=True)
class Projection:
    nodes: list[tuple]
    contents: list[tuple]
    edges: list[tuple]
    outbox: list[tuple]
    influence: list[tuple]

    def canonical(self, *, stable: bool = False) -> str:
        """规范化 JSON。

        `stable=True` 会丢掉**自增 id**（node_content.id / outbox.id /
        influence.id）——两份副本按各自的插入顺序生成自增 id，必然不同；
        比较语义时不该被它们干扰（`replica.verify` 用的就是这个）。
        """
        if stable:
            return json.dumps(
                {
                    "nodes": sorted(self.nodes),
                    "contents": sorted(r[1:] for r in self.contents),
                    "edges": sorted(self.edges),
                    "outbox": sorted(r[1:] for r in self.outbox),
                    "influence": sorted(r[1:] for r in self.influence),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                "nodes": sorted(self.nodes),
                "contents": sorted(self.contents),
                "edges": sorted(self.edges),
                "outbox": sorted(self.outbox),
                "influence": sorted(self.influence),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def clear_projection(db: DB) -> None:
    with db.conn:
        for t in PROJECTION_TABLES:
            db.conn.execute(f"DELETE FROM {t}")


def dump_projection(db: DB) -> Projection:
    return Projection(
        nodes=[tuple(r) for r in db.q("SELECT * FROM nodes")],
        contents=[tuple(r) for r in db.q("SELECT * FROM node_content")],
        edges=[tuple(r) for r in db.q("SELECT * FROM edges")],
        outbox=[tuple(r) for r in db.q("SELECT * FROM outbox")],
        influence=[tuple(r) for r in db.q("SELECT * FROM influence")],
    )


def entity_id(name: str) -> str:
    """实体的稳定 id。同名同 id——这是跨消息合并的唯一依据。"""
    h = hashlib.sha256(("entity\x00" + name.strip()).encode()).hexdigest()[:16]
    return f"e:{h}"


def apply_payload(conn: sqlite3.Connection, row: sqlite3.Row, payload: dict) -> str:
    """**唯一的**落图入口。实时抽取和恢复重放都必须走这里。

    分开写两份的话，重放出来的图会少一批节点——而这个差异要等
    真正的灾难恢复时才会被发现。返回 episode 节点 id。
    """
    kind = row["kind"]
    ts = row["ts"]
    nid = node_id_for(row["source"], row["ext_id"])
    scope = "self:her" if kind == "inner" else "world"

    conn.execute(
        "INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (nid, "episode" if kind == "msg_in" else "self", ts, scope,
         1.0, 1.0, ts))
    conn.execute("INSERT INTO node_content(node_id, content, created_ts)"
                 " VALUES(?,?,?)", (nid, str(payload.get("summary", "")).strip(), ts))

    def touch(nid_: str, name: str) -> None:
        conn.execute(
            "INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (nid_, "entity", ts, "world", 1.0, 1.0, ts))
        conn.execute("INSERT INTO node_content(node_id, content, created_ts)"
                     " VALUES(?,?,?)", (nid_, name, ts))

    def link(a: str, b: str, w: float, edge_kind: str) -> None:
        conn.execute(
            "INSERT INTO edges(src, dst, w, kind, t_created, last_coact)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(src, dst, kind) DO UPDATE SET"
            "   w = w + excluded.w, last_coact = excluded.last_coact",
            (a, b, w, edge_kind, ts, ts))

    for e in payload.get("entities") or []:
        name = str(e.get("name", "")).strip() if isinstance(e, dict) else str(e).strip()
        if not name:
            continue
        eid = entity_id(name)
        touch(eid, name)
        # 这件事提到了这个实体——边是「共同出现」，不是「是」。
        link(nid, eid, 1.0, "mentions")

    for cl in payload.get("claims") or []:
        if not isinstance(cl, dict):
            continue
        subj = str(cl.get("subject", "")).strip()
        obj = str(cl.get("object", "")).strip()
        pred = str(cl.get("predicate", "")).strip()
        if not (subj and obj and pred):
            continue
        a, b = entity_id(subj), entity_id(obj)
        touch(a, subj)
        touch(b, obj)
        link(a, b, 0.5, f"rel:{pred[:24]}")
    return nid


def _apply(conn: sqlite3.Connection, row: sqlite3.Row, response: str) -> None:
    """把一条"事件 + 它的模型结果"落成投影。

    结果可能是 JSON（extract.py 的约定），也可能是纯文本（早期数据）。
    两种都得认——否则老事件重放时会被静默丢掉。
    """
    if row["kind"] not in ("msg_in", "inner"):
        return
    try:
        obj = json.loads(response[response.index("{"): response.rindex("}") + 1])
        if not isinstance(obj, dict) or not str(obj.get("summary", "")).strip():
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        obj = {"summary": response, "entities": [], "claims": []}
    apply_payload(conn, row, obj)


def restore(db: DB, *, until_event_id: int | None = None) -> int:
    """恢复重放：清空投影，只用日志集重建。返回处理的事件条数。

    不调用任何模型。缺 model_calls 就抛 MissingModelResult。
    **已删除的事件不参与重放**：它连同抽取响应一起被清过，再读一次就等于
    把删掉的内容造回来。
    """
    calls = {
        r["event_id"]: r["response"]
        for r in db.q(
            "SELECT event_id, response FROM model_calls"
            " WHERE kind='extract' AND event_id IS NOT NULL ORDER BY id"
        )
    }

    sql = "SELECT * FROM events WHERE kind IN ('msg_in','inner')"
    args: list[object] = []
    if until_event_id is not None:
        sql += " AND id <= ?"
        args.append(until_event_id)
    sql += " ORDER BY id"

    dead = {r["event_id"] for r in db.q("SELECT event_id FROM redactions")}

    n = 0
    # 整个重放是一个事务：崩在中途就保持旧投影，不留半成品。
    with Writer(db).txn() as c:
        for t in PROJECTION_TABLES:
            c.execute(f"DELETE FROM {t}")
        for row in db.conn.execute(sql, args).fetchall():
            if row["id"] in dead:      # 删掉的不参与重放
                continue
            response = calls.get(row["id"])
            if response is None:
                raise MissingModelResult(
                    f"event {row['id']} ({row['source']}/{row['ext_id']}) 没有已保存的模型结果；"
                    "恢复重放不允许重新调用模型"
                )
            _apply(c, row, response)
            n += 1
    return n


def trace(db: DB, outbox_id: int) -> dict:
    """一条回复的来由：用了哪些节点、哪一次模型结果。"""
    box = db.one("SELECT * FROM outbox WHERE id=?", (outbox_id,))
    if box is None:
        raise KeyError(f"outbox {outbox_id} 不存在")
    rows = db.q(
        "SELECT i.node_id, i.w, i.model_call_id, n.kind, n.scope,"
        "       (SELECT content FROM node_content c WHERE c.node_id=i.node_id"
        "        ORDER BY c.id DESC LIMIT 1) AS content"
        " FROM influence i LEFT JOIN nodes n ON n.id = i.node_id"
        " WHERE i.outbox_id=? ORDER BY i.w DESC",
        (outbox_id,),
    )
    return {
        "outbox": dict(box),
        "inputs": [dict(r) for r in rows],
    }
