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

    def pretty(self) -> str:
        return (f"查询 {self.n_queries} 条 · recall@1 {self.r1:.2f} · "
                f"@5 {self.r5:.2f} · @10 {self.r10:.2f} · "
                f"对照命中 {self.control_hit:.2f} · 平均候选 {self.avg_candidates:.0f}")


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
    for q in fs.queries:
        got = [h.node_id for h in search(conn, q.text, k=k)]
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
                  avg_candidates=st.mean(cands) if cands else 0.0)


def main(days: int = 180, seeds: tuple[int, ...] = (7, 11, 23)) -> None:
    print(f"窗口 {days} 天 · {len(seeds)} 个种子 · 间接提问的词面召回\n")
    for seed in seeds:
        fs = generate(days=days, seed=seed)
        print(f"  seed={seed:3d}  " + evaluate(fs).pretty())


if __name__ == "__main__":
    main()
