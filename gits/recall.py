"""把检索接进真实 schema：node_content + nodes(r/s) + FTS。

这一层是「她记得」的最后一公里。三件事：

    文本    取 node_content 的最新版本（版本链：superseded_by IS NULL）
    可提取性 节点上的 s 与 last_touch → FSRS 的 r（不重新计算历史）
    死活    被取代的旧版本 r 直接置 0——它不该被想起

不引入新依赖：FTS 是二元组、r 是一行公式。
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.db import DB
from gits.fsrs import DAY, retrievability
from gits.scope import visible
from gits.search import index_node, search


@dataclass(frozen=True)
class Recalled:
    node_id: str
    score: float
    sim: float
    r: float
    text: str
    age_ms: int

    def line(self) -> str:
        """进 C 段的形状。短、带时间感、不解释来源。

        **不带列表符号**——`render_c` 会加。两处都加就会拼出 `- - 1 小时前`。
        """
        return f"{_age(self.age_ms)}：{self.text}"


def _age(ms: int) -> str:
    d = max(0, ms) / DAY
    if d < 1:
        return f"{int(max(1, d * 24))} 小时前"
    if d < 30:
        return f"{int(d)} 天前"
    if d < 365:
        return f"{int(d / 30)} 个月前"
    return f"{int(d / 365)} 年前"


def latest_content(db: DB, node_id: str):
    """最新版本的 (content, id, superseded_by, created_ts)。"""
    return db.one(
        "SELECT id, content, superseded_by, absorbed_into, created_ts"
        " FROM node_content WHERE node_id=? ORDER BY id DESC LIMIT 1", (node_id,))


def is_alive(db: DB, node_id: str) -> bool:
    row = latest_content(db, node_id)
    return bool(row) and row["superseded_by"] is None


def reindex(db: DB, *, limit: int | None = None) -> int:
    """把 node_content 灌进 FTS。**派生索引，随时可以整表 drop 重建。**"""
    with db.conn:
        db.conn.execute("DELETE FROM node_fts") if _has_fts(db) else None
    n = 0
    sql = "SELECT node_id, content FROM node_content ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db.conn:
        for row in db.q(sql):
            index_node(db.conn, row["node_id"], row["content"])
            n += 1
    return n


def _has_fts(db: DB) -> bool:
    return bool(db.one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_fts'"))


def recall(db: DB, query: str, *, actor: str, context: str = "",
           now_ms: int, k: int = 10) -> list[Recalled]:
    """她此刻能想起的 k 条。"""
    def r_of(nid: str) -> float:
        row = db.one("SELECT s, last_touch FROM nodes WHERE id=?", (nid,))
        if not row or not row["s"] or row["last_touch"] is None:
            return 0.0
        if not is_alive(db, nid):
            return 0.0                    # 被取代的版本不该被想起
        elapsed_days = max(0.0, (now_ms - row["last_touch"]) / DAY)
        return retrievability(elapsed_days, float(row["s"]))

    hits = search(db.conn, query, k=k * 3, context=context, r_of=r_of,
                  scopes=visible(actor))
    out: list[Recalled] = []
    for h in hits:
        row = latest_content(db, h.node_id)
        # 被推翻的（改了口的）和**被吸收的**（压进摘要的）都进不了检索。
        # 后者是漏斗的本意：不可检索，但还查得到。见 docs/research/funnel.md。
        if row is None or row["superseded_by"] is not None:
            continue
        if row["absorbed_into"] is not None:
            continue
        out.append(Recalled(
            node_id=h.node_id, score=h.score, sim=h.sim,
            r=r_of(h.node_id), text=str(row["content"]),
            age_ms=max(0, now_ms - int(row["created_ts"])),
        ))
        if len(out) >= k:
            break
    return out


def lines(rows: list[Recalled]) -> tuple[str, ...]:
    return tuple(r.line() for r in rows)
