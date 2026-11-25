"""把合成事实灌进真实 schema，用来验证「检索接进 ghost」这条链路。

不是为了评测——评测已经在 eval_search 里做过。这里是**端到端复现**：
同一份数据、同样的三层能力，但走的是生产代码路径（nodes / node_content /
FTS / recall），而不是一个内存里的玩具索引。
"""

from __future__ import annotations

from pathlib import Path

from gits.db import DB, Writer
from gits.fsrs import Memory, Rating
from gits.recall import reindex
from synth.facts import DAY, Facts


def load(fs: Facts, db_path: str | Path, *, upto_ms: int) -> DB:
    """把 `upto_ms` 之前出现的事实写进库。

    节点上的 s / last_touch 由 FSRS **重放**提及时间轴得到——
    线上也是这样：不在检索时重算历史，只在写入时更新一次。
    """
    db = DB(db_path)
    w = Writer(db)
    names = {e.id: e.name for e in fs.entities}

    with w.txn() as c:
        c.execute("DELETE FROM nodes")
        c.execute("DELETE FROM node_content")

    for f in fs.facts:
        if f.born >= upto_ms:
            continue
        times = sorted(m.ts for m in fs.mentions if m.fact == f.id and m.ts < upto_ms)
        if not times:
            continue
        mem = Memory()
        for t in times:
            mem.review(t, Rating.GOOD)

        text = f"{names.get(f.entity, '')} {f.predicate} {f.value}"
        with w.txn() as c:
            c.execute(
                "INSERT INTO nodes(id, kind, born_ts, scope, d, s, r, last_touch,"
                " centrality) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET s=excluded.s, d=excluded.d,"
                " last_touch=excluded.last_touch",
                (f.id, "fact", f.born, "world", mem.d, mem.s, mem.r(times[-1]),
                 times[-1], 0.0),
            )
            content_id = c.execute(
                "INSERT INTO node_content(node_id, content, created_ts) VALUES(?,?,?)",
                (f.id, text, times[0]),
            ).lastrowid

        # 被取代的版本：同一节点的内容链断了，标记 superseded_by
        if f.superseded_by:
            later = [m.ts for m in fs.mentions
                     if m.fact == f.superseded_by and m.ts < upto_ms]
            if later:
                with w.txn() as c:
                    c.execute("UPDATE node_content SET superseded_by=?"
                              " WHERE id=?", (content_id + 1, content_id))

    reindex(db)
    db.close()
    return DB(db_path)
