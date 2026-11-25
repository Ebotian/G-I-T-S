"""端到端：走生产代码路径（nodes / node_content / FTS / recall）跑同一批查询。

它不追求和内存玩具索引"完全一致"——两者本就不该一致：生产路径带 `r`，
玩具索引不带。要确认的是**生产路径复现了评测里的质量**：

    答案总能进 top-5，且 @1 在同一个量级。

不一致恰恰说明接线错了。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

from gits.recall import recall
from synth.bridge import load
from synth.eval_search import build_index
from synth.facts import Facts
from gits.search import search as raw_search


def compare(fs: Facts, *, n_samples: int = 12) -> dict:
    qs = fs.queries[:: max(1, len(fs.queries) // n_samples)][:n_samples]
    prod1 = prod5 = toy1 = 0

    for q in qs:
        tmp = Path(mkdtemp()) / "mem.db"
        db = load(fs, tmp, upto_ms=q.ts)
        try:
            prod = recall(db, q.text, context=q.context, now_ms=q.ts, k=5)
            conn = build_index(fs, upto_ms=q.ts)
            toy = raw_search(conn, q.text, k=5, context=q.context)
        finally:
            db.close()

        if prod and prod[0].node_id == q.answer:
            prod1 += 1
        if any(r.node_id == q.answer for r in prod):
            prod5 += 1
        if toy and toy[0].node_id == q.answer:
            toy1 += 1

    n = len(qs) or 1
    return {"n": len(qs), "prod_r1": prod1 / n, "prod_r5": prod5 / n,
            "toy_r1_no_r": toy1 / n}
