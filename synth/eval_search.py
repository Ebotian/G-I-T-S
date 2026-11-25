"""检索基线：**只看词面能拿到多少**。

这个评测存在的意义不是"跑出好看的数字"，而是给向量召回定一个地板价：
如果间接提问的词面召回是 0，那"要不要上 embedding"就不再是一个口味问题。

同时还跑一个**对照**：用事实自己的字面去搜，必须能命中。
否则"0 分"可能只是索引写坏了，而不是方法不行。
"""

from __future__ import annotations

import sqlite3
import statistics as st
from dataclasses import dataclass

from gits.search import index_node, search
from synth.facts import DAY, Facts, generate


@dataclass
class Report:
    n_queries: int
    r1: float
    r5: float
    r10: float
    control_hit: float
    avg_candidates: float
    ctx_r1: float = 0.0
    ctx_r5: float = 0.0
    ctx_r10: float = 0.0
    #: 「找对了东西没有」——top-k 里只要有一条属于正确实体就算命中。
    #: 它和「找对了哪一条」是两件事：同一个实体的多条事实字面常常一模一样。
    entity_hit: float = 0.0

    def pretty(self) -> str:
        return (f"查询 {self.n_queries} 条 · 裸问 @1 {self.r1:.2f}/@5 {self.r5:.2f}"
                f" · **带上下文 @1 {self.ctx_r1:.2f}/@5 {self.ctx_r5:.2f}"
                f"/@10 {self.ctx_r10:.2f}** · 找对实体 {self.entity_hit:.2f}"
                f" · 对照 {self.control_hit:.2f}")


def build_index(fs: Facts) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    names = {e.id: e.name for e in fs.entities}
    for f in fs.facts:
        text = f"{names.get(f.entity, '')} {f.predicate} {f.value}"
        index_node(conn, f.id, text)
    return conn


def evaluate(fs: Facts, *, k: int = 10) -> Report:
    conn = build_index(fs)
    hits1 = hits5 = hits10 = 0
    control_hits = 0
    cands = []

    by_id = {f.id: f for f in fs.facts}
    c1 = c5 = c10 = ent = 0
    for q in fs.queries:
        got = [h.node_id for h in search(conn, q.text, k=k)]
        withctx = [h.node_id for h in search(conn, f"{q.context} {q.text}", k=k)]
        if set(withctx[:k]) & set(q.candidates):
            ent += 1
        if q.answer in withctx[:1]:
            c1 += 1
        if q.answer in withctx[:5]:
            c5 += 1
        if q.answer in withctx:
            c10 += 1
        cands.append(len(q.candidates))
        if q.answer in got[:1]:
            hits1 += 1
        if q.answer in got[:5]:
            hits5 += 1
        if q.answer in got:
            hits10 += 1

        f = by_id[q.answer]
        ctrl = [h.node_id for h in search(conn, f"{f.predicate} {f.value}", k=k)]
        if f.id in ctrl:
            control_hits += 1

    n = len(fs.queries) or 1
    return Report(n_queries=len(fs.queries), r1=hits1 / n, r5=hits5 / n,
                  r10=hits10 / n, control_hit=control_hits / n,
                  avg_candidates=st.mean(cands) if cands else 0.0,
                  ctx_r1=c1 / n, ctx_r5=c5 / n, ctx_r10=c10 / n,
                  entity_hit=ent / n)


def main(days: int = 180, seeds: tuple[int, ...] = (7, 11, 23)) -> None:
    print(f"窗口 {days} 天 · {len(seeds)} 个种子 · 间接提问的词面召回\n")
    for seed in seeds:
        fs = generate(days=days, seed=seed)
        print(f"  seed={seed:3d}  " + evaluate(fs).pretty())


if __name__ == "__main__":
    main()
