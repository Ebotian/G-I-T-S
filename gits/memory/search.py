"""检索：词面 + 图 + 打分。向量先留一个钩子。

分词用**字符二元组**，不用 jieba。理由不是省事：

    jieba 的默认词典常驻 ~50 MB RSS，而 ghost 的依赖预算只有 120 MB
    （docs/research/stats-libs.md §6）。词表还会漏人名、新词、代码片段。
    二元组无词典、无 OOV、内存为零。代价是索引大一点——我们一年 4 MB，无所谓。

打分（DESIGN §5.1.4）：

    score = w_sim·sim + w_r·r + w_e·energy + w_c·centrality + w_rec·recency

**它不是概率，也不该被当成概率**。分数只用来排序，绝对数值没有意义。
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> str:
    """中文按二元组、拉丁按词。返回空格分隔的串，直接喂 FTS5。"""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        for tok in ASCII_WORD.findall(text[pos:m.start()]):
            out.append(tok.lower())
        run = m.group(0)
        if CJK.fullmatch(run):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            out.append(run.lower())
        pos = m.end()
    for tok in ASCII_WORD.findall(text[pos:]):
        out.append(tok.lower())
    return " ".join(out)


def ensure_fts(conn: sqlite3.Connection) -> None:
    """FTS5 与主表分开存：主表是真相，这张是索引，随时可以 drop 重建。"""
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5("
        " node_id UNINDEXED, text, tokenize='unicode61')"
    )


def index_node(conn: sqlite3.Connection, node_id: str, text: str) -> None:
    ensure_fts(conn)
    conn.execute("DELETE FROM node_fts WHERE node_id=?", (node_id,))
    conn.execute("INSERT INTO node_fts(node_id, text) VALUES(?,?)",
                 (node_id, tokenize(text)))


def drop_index(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS node_fts")


def bm25_to_sim(rank: float) -> float:
    """把 FTS5 的 bm25 压到 [0,1)。

    ⚠️ **方向**：bm25 越负越好，所以相似度必须随 |rank| **增大**。
    我第一版写成 `1/(1+|rank|)`，方向反了——匹配越好的反而分数越低，
    于是检索永远把最差的排在最前面。它不报错，只是把结果全都排反。

    这是**单调映射，不是校准过的相似度**。用途只有一个：让不同来源的分数
    能放在同一个排序公式里。
    """
    a = abs(rank)
    # 加一个极小量：bm25 在小语料里可能正好是 0（词在所有文档里都有，IDF=0），
    # 但那条文档**确实命中了**。映射成 0 会让它和"没命中"无法区分，
    # 排序也就退化成插入顺序。0.001 不影响单调性，只保住"命中"这件事。
    return (a + 1e-3) / (1.0 + a)


@dataclass(frozen=True)
class Weights:
    sim: float = 0.55
    r: float = 0.25
    energy: float = 0.10
    centrality: float = 0.05
    recency: float = 0.05


def combine(*, sim: float = 0.0, r: float = 0.0, energy: float = 0.0,
            centrality: float = 0.0, recency: float = 0.0,
            w: Weights = Weights()) -> float:
    """加权和。刻意保持线性——可解释、可消融、调参不玄学。"""
    return (w.sim * sim + w.r * r + w.energy * energy
            + w.centrality * centrality + w.recency * recency)


@dataclass(frozen=True)
class Hit:
    node_id: str
    score: float
    sim: float
    why: str


def search(conn: sqlite3.Connection, query: str, *, k: int = 10,
           r_of: Callable[[str], float] | None = None,
           energy_of: Callable[[str], float] | None = None,
           centrality_of: Callable[[str], float] | None = None,
           recency_of: Callable[[str], float] | None = None,
           vec_of: Callable[[str], float] | None = None,
           candidates_of: Callable[[str], Iterable[tuple[str, float]]] | None = None,
           context: str = "",
           scopes: Sequence[str] | None = None,
           w: Weights = Weights()) -> list[Hit]:
    """检索。词面是主路，向量是**另一条召回通道**（不是重排钩子）。

    `candidates_of(query)` 返回 [(node_id, sim)]，与词面候选**并集**。
    这个区别很重要：只做重排的话，词面没召回的节点永远不会被向量救回来——
    而"间接提问"恰恰全是这种情况。
    """
    ensure_fts(conn)
    # ★ 上下文扩展：指代是靠刚聊过的东西解析的，不是靠记忆库（retrieval.md §4）。
    # 它不需要任何模型，把最近几轮原文拼进来就行。
    tokens = tokenize((context + " " + query).strip() if context else query)
    if not tokens.strip():
        return []

    # ★ 可见域在这里挡一次。不传 scopes 就是"全部可见"，
    # 但生产路径（recall）永远会传——它没有默认值。
    clause, args = "", []
    if scopes:
        clause = (" AND node_id IN (SELECT id FROM nodes WHERE scope IN (%s))"
                  % ",".join("?" * len(scopes)))
        args = list(scopes)
    rows = conn.execute(
        "SELECT node_id, bm25(node_fts) AS rank FROM node_fts"
        " WHERE node_fts MATCH ?" + clause + " ORDER BY rank LIMIT ?",
        [" OR ".join(tokens.split()), *args, max(k * 5, 20)],
    ).fetchall()

    merged: dict[str, tuple[float, list[str]]] = {}
    for row in rows:
        merged[row["node_id"]] = (bm25_to_sim(float(row["rank"])), ["fts"])

    if candidates_of is not None:
        for nid, sim in candidates_of(query):
            old = merged.get(nid)
            if old is None:
                merged[nid] = (float(sim), ["vec"])
            elif sim > old[0]:
                merged[nid] = (float(sim), old[1] + ["vec"])

    hits: list[Hit] = []
    for nid, (sim, why) in merged.items():
        if vec_of is not None:
            v = vec_of(nid)
            if v > sim:
                sim = v
                why = why + ["vec"]
        s = combine(
            sim=sim,
            r=0.0 if r_of is None else r_of(nid),
            energy=0.0 if energy_of is None else energy_of(nid),
            centrality=0.0 if centrality_of is None else centrality_of(nid),
            recency=0.0 if recency_of is None else recency_of(nid),
            w=w,
        )
        hits.append(Hit(node_id=nid, score=s, sim=sim, why="+".join(why)))

    hits.sort(key=lambda h: -h.score)
    return hits[:k]
