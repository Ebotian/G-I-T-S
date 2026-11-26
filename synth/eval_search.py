"""检索评测：三层能力分别测。

    1. 裸问            应给 0——间接提问和事实之间没有字面重叠
    2. + 上下文        应到 0.6~0.8——指代靠刚聊过的东西解析
    3. + 上下文 + r    应把 @1 拉起来——"是哪一条"交给可提取性，不交给相似度

第 3 层是这一轮的重点：同一实体的多条事实字面完全一样，
**相似度在这里没有信息，能区分它们的只有"哪条还活着、哪条还记得住"**。
"""

from __future__ import annotations

import sqlite3
import statistics as st
from dataclasses import dataclass

from gits.memory.fsrs import Memory, Rating
from gits.memory.search import index_node, search
from synth.facts import DAY, Facts, generate


@dataclass
class Report:
    n_queries: int
    r1: float
    r5: float
    r10: float
    control_hit: float
    ctx_r1: float
    ctx_r5: float
    ctx_r10: float
    entity_hit: float
    full_r1: float
    full_r5: float
    full_r10: float

    def pretty(self) -> str:
        return (f"r@1 裸问 {self.r1:.2f} → 上下文 {self.ctx_r1:.2f}"
                f" → +r {self.full_r1:.2f}   |"
                f"  r@5 {self.r5:.2f} → {self.ctx_r5:.2f} → {self.full_r5:.2f}   |"
                f"  r@10 {self.r10:.2f} → {self.ctx_r10:.2f} → {self.full_r10:.2f}"
                f"   |  找对实体 {self.entity_hit:.2f}")


def build_index(fs: Facts, *, upto_ms: int | None = None) -> sqlite3.Connection:
    """`upto_ms` 用来把「未来才出现的事实」挡在索引外——不然评测会作弊。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    names = {e.id: e.name for e in fs.entities}
    for f in fs.facts:
        if upto_ms is not None and f.born >= upto_ms:
            continue
        index_node(conn, f.id, f"{names.get(f.entity, '')} {f.predicate} {f.value}")
    return conn


def _retrievability_at(fs: Facts, fact_id: str, qt: int) -> float:
    """FSRS 在提问时刻的可提取性。用真实提及时间轴推。"""
    times = sorted(m.ts for m in fs.mentions if m.fact == fact_id and m.ts < qt)
    m = Memory()
    for t in times:
        m.review(t, Rating.GOOD)
    return m.r(qt)


def evaluate(fs: Facts, *, k: int = 10) -> Report:
    conn = build_index(fs)
    by_id = {f.id: f for f in fs.facts}
    n = len(fs.queries) or 1

    hits = {"bare": [0, 0, 0], "ctx": [0, 0, 0], "full": [0, 0, 0]}
    ent = ctrl = 0

    for q in fs.queries:
        dead = {f.id for f in fs.facts
                if f.superseded_by and any(m.fact == f.superseded_by and m.ts < q.ts
                                           for m in fs.mentions)}

        def r_of(nid: str) -> float:
            if nid in dead:
                return 0.0           # 已经被取代的版本不该再被想起
            return _retrievability_at(fs, nid, q.ts)

        for tag, got in (
            ("bare", search(conn, q.text, k=k)),
            ("ctx", search(conn, q.text, k=k, context=q.context)),
            ("full", search(conn, q.text, k=k, context=q.context, r_of=r_of)),
        ):
            ids = [h.node_id for h in got]
            if q.answer in ids[:1]:
                hits[tag][0] += 1
            if q.answer in ids[:5]:
                hits[tag][1] += 1
            if q.answer in ids:
                hits[tag][2] += 1
            if tag == "full" and set(ids) & set(q.candidates):
                ent += 1

        f = by_id[q.answer]
        if f.id in [h.node_id for h in
                    search(conn, f"{f.predicate} {f.value}", k=k)]:
            ctrl += 1

    return Report(
        n_queries=len(fs.queries),
        r1=hits["bare"][0] / n, r5=hits["bare"][1] / n, r10=hits["bare"][2] / n,
        control_hit=ctrl / n,
        ctx_r1=hits["ctx"][0] / n, ctx_r5=hits["ctx"][1] / n, ctx_r10=hits["ctx"][2] / n,
        entity_hit=ent / n,
        full_r1=hits["full"][0] / n, full_r5=hits["full"][1] / n,
        full_r10=hits["full"][2] / n,
    )


def main(days: int = 180, seeds: tuple[int, ...] = (7, 11, 23)) -> None:
    print(f"窗口 {days} 天 · {len(seeds)} 个种子 · 三层对照\n")
    for seed in seeds:
        fs = generate(days=days, seed=seed)
        print(f"  seed={seed:3d}  " + evaluate(fs).pretty())


if __name__ == "__main__":
    main()
