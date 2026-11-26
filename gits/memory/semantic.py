"""语义解析：实体别名。**不引入向量。**

问题很具体：entity_id 是精确字符串，于是

    开会 / 会议 / 那个会      三个簇
    老板 / 领导               两个簇

而漏斗的复现次数是按簇算的——同一个东西被拆成三份，复现永远到不了阈值，
于是它永远不会被概括。**同义词会把记忆漏斗悄悄废掉。**

两条路，都不需要向量：

  别名表     模型在夜里压缩时顺带提一句（那一步本来就要调），
             有事件当依据、确定性落库、可重放。
  共现推断   两个实体和同一批东西一起出现，可能是一回事。**只提议，不合。**

为什么共现只提议：它同时会命中和它「相关但不同」的东西——
开会和咖啡都常常和「他」一起出现，但它们不是一回事。
拿不准就不合，让人（或模型）看一眼。
"""

from __future__ import annotations

import json

from gits.core.db import DB, Writer, now_ms

#: 共现推断的默认门槛。只用来产生候选，不直接合并。
PROXIMITY = 0.5
MIN_MENTIONS = 2
MAX_CHAIN = 8


def resolve(db: DB, eid: str) -> str:
    """把别名追到根。**带环保护和深度上限**——链是可以被写坏的。"""
    seen: set[str] = set()
    cur = eid
    for _ in range(MAX_CHAIN):
        if cur in seen:
            break
        seen.add(cur)
        row = db.one("SELECT canonical FROM entity_alias"
                     " WHERE alias_id=? AND superseded_by IS NULL", (cur,))
        if row is None:
            break
        cur = str(row["canonical"])
    return cur


def family(db: DB, eid: str) -> set[str]:
    """这个实体连同所有指向它的别名。查图时用这一组去匹配。"""
    root = resolve(db, eid)
    out = {root}
    for r in db.q("SELECT alias_id FROM entity_alias"
                  " WHERE canonical=? AND superseded_by IS NULL", (root,)):
        out.add(str(r["alias_id"]))
    return out


def merge(db: DB, alias: str, canonical: str, *, source: str = "model",
          certainty: float = 1.0, now: int | None = None,
          because: list[int] | None = None) -> int:
    """记一条别名。**走事件 → 投影**，和其他东西同一条重放路径。"""
    from gits.core import project

    now = now or now_ms()
    if alias == canonical or resolve(db, canonical) == alias:
        raise ValueError("别名会形成环")
    payload = {"alias": alias, "canonical": resolve(db, canonical),
               "source": source, "certainty": certainty,
               "because": because or []}
    ext = f"alias-{alias}-{now}"
    ev, _ = Writer(db).append_event(
        source="alias", kind="alias", ext_id=ext, ts=now, recv_ts=now,
        scope="world",
        payload=json.dumps(payload, ensure_ascii=False).encode())
    row = {"kind": "alias", "source": "alias", "ext_id": ext, "ts": now,
           "scope": "world"}
    with Writer(db).txn() as c:
        project._apply(c, row, json.dumps(payload, ensure_ascii=False))
    return ev


def contexts(db: DB, *, kind: str = "mentions") -> dict[str, set[str]]:
    """每个实体和哪些实体一起出现过。共现推断的原料。"""
    by_src: dict[str, set[str]] = {}
    for r in db.q("SELECT src, dst FROM edges WHERE kind=?", (kind,)):
        by_src.setdefault(str(r["src"]), set()).add(str(r["dst"]))
    ctx: dict[str, set[str]] = {}
    for dsts in by_src.values():
        for a in dsts:
            ctx.setdefault(a, set()).update(d for d in dsts if d != a)
    return ctx


def propose(db: DB, *, threshold: float = PROXIMITY,
            limit: int = 10) -> list[tuple[str, str, float]]:
    """共现推断的候选 [(a, b, score)]。**这只是候选，不写库。**"""
    ctx = contexts(db)
    mentioned = {str(r["dst"]) for r in db.q(
        "SELECT dst, count(*) n FROM edges WHERE kind='mentions'"
        " GROUP BY dst HAVING n >= ?", (MIN_MENTIONS,))}
    keys = sorted(k for k in ctx if k in mentioned)
    out: list[tuple[str, str, float]] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if resolve(db, a) == resolve(db, b):
                continue
            sa, sb = ctx[a], ctx[b]
            if not sa or not sb:
                continue
            score = len(sa & sb) / len(sa | sb)
            if score >= threshold:
                out.append((a, b, round(score, 3)))
    out.sort(key=lambda t: -t[2])
    return out[:limit]
