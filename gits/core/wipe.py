"""清空内容，保留机制。

把种子、人格快照、漂移记录标成删除；表、代码、调度原样留着。

为什么用 redact 而不是 DELETE 行：事件是唯一真相，
「曾经有过一段初始人格，后来被清掉了」本身也是事实。
删掉行会让这句话没有依据——redactions 表就是为这件事存在的。
"""

from __future__ import annotations

from gits.core.db import DB, Writer, now_ms
from gits.core.scope import self_of

#: 属于「内容」的事件类型。机制不在这里面。
CONTENT_KINDS = ("seed", "persona", "drift", "attention", "inner",
                 "consolidate", "funnel")


def wipe(db: DB, *, actor: str = "her", now: int | None = None,
         kinds: tuple[str, ...] = CONTENT_KINDS) -> dict[str, int]:
    """把这个实例的内容清空。**机制不动**：表在、代码在、调度在。"""
    now = now or now_ms()
    scope = self_of(actor)
    marks = ",".join("?" * len(kinds))
    rows = db.q(
        "SELECT id, kind FROM events WHERE kind IN (" + marks + ")"
        " AND scope=? AND payload IS NOT NULL ORDER BY id",
        (*kinds, scope))
    counts: dict[str, int] = {}
    for r in rows:
        Writer(db).redact_event(int(r["id"]))
        counts[str(r["kind"])] = counts.get(str(r["kind"]), 0) + 1

    # 注意力权重要一起清：它是内容（种下来的偏好），不是机制。
    # 不清的话 wipe 之后 peers 还显示着旧偏好——「清干净了」就是假的。
    n_att = db.scalar("SELECT count(*) FROM attention WHERE actor_id=?", (actor,))
    with Writer(db).txn() as c:
        c.execute("DELETE FROM attention WHERE actor_id=?", (actor,))
    if n_att:
        counts["attention_weights"] = int(n_att)

    # 内容没了，投影也得跟着空——否则重放出来的图还留着旧节点
    if counts:
        with Writer(db).txn() as c:
            c.execute("DELETE FROM edges WHERE src IN"
                      " (SELECT id FROM nodes WHERE scope=?)", (scope,))
            c.execute("DELETE FROM node_content WHERE node_id IN"
                      " (SELECT id FROM nodes WHERE scope=?)", (scope,))
            c.execute("DELETE FROM nodes WHERE scope=?", (scope,))
            # FTS 是可选索引（ensure_fts 才建），别假设它存在
            from gits.memory.search import ensure_fts

            ensure_fts(c)
            c.execute("DELETE FROM node_fts WHERE node_id NOT IN"
                      " (SELECT id FROM nodes)")
    return counts


def states(db: DB, *, actor: str = "her") -> dict[str, int]:
    """还剩多少内容。全 0 就是「干净、缺内容」的状态。"""
    scope = self_of(actor)
    marks = ",".join("?" * len(CONTENT_KINDS))
    rows = db.q(
        "SELECT kind, count(*) n FROM events WHERE kind IN (" + marks + ")"
        " AND scope=? AND payload IS NOT NULL GROUP BY kind",
        (*CONTENT_KINDS, scope))
    return {str(r["kind"]): int(r["n"]) for r in rows}
