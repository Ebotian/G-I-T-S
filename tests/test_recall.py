"""端到端：从合成事实到「她记得」，走生产代码路径。

这一层的意义：前面所有评测都跑在内存玩具索引上，快但不算数。
这里用真实的 nodes / node_content / FTS / recall 走一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.context import PersonaSeed, RelationSnapshot, TurnContext, assemble
from gits.recall import lines, recall, reindex
from synth.bridge import load
from synth.eval_e2e import compare
from synth.facts import generate

FS = generate(days=60, seed=7)


def _db(tmp_path: Path):
    q = FS.queries[-1]
    return load(FS, tmp_path / "mem.db", upto_ms=q.ts), q


# ── 基本行为 ────────────────────────────────────────────────
def test_裸问想不起来(tmp_path: Path) -> None:
    db, q = _db(tmp_path)
    try:
        assert recall(db, "那个老是出问题的家伙", actor="her", now_ms=q.ts) == []
    finally:
        db.close()


def test_有上下文就想得起来(tmp_path: Path) -> None:
    db, q = _db(tmp_path)
    try:
        rows = recall(db, q.text, actor="her", context=q.context, now_ms=q.ts, k=5)
        assert rows, "补上上下文之后必须能想起来"
        assert rows[0].r > 0
        assert rows[0].text
    finally:
        db.close()


def test_她的那一行不带列表符号(tmp_path: Path) -> None:
    """`render_c` 会加 `- `；两处都加就成了 `- - 1 小时前`。"""
    db, q = _db(tmp_path)
    try:
        rows = recall(db, q.text, actor="her", context=q.context, now_ms=q.ts, k=1)
        assert not rows[0].line().startswith("-")
        assert "：" in rows[0].line()
    finally:
        db.close()


def test_被取代的版本不会被想起(tmp_path: Path) -> None:
    db = load(FS, tmp_path / "m.db", upto_ms=FS.end)
    try:
        dead = [r for r in db.q("SELECT node_id FROM node_content"
                                " WHERE superseded_by IS NOT NULL")]
        assert dead, "这份数据里应该有被取代的版本"
        nid = dead[0]["node_id"]
        rows = recall(db, "想换掉", actor="her", context="物", now_ms=FS.end, k=50)
        assert all(r.node_id != nid for r in rows) or all(
            r.r == 0 for r in rows if r.node_id == nid)
    finally:
        db.close()


def test_重放索引是幂等的(tmp_path: Path) -> None:
    db, q = _db(tmp_path)
    try:
        n1 = db.scalar("SELECT count(*) FROM node_fts")
        reindex(db)
        assert db.scalar("SELECT count(*) FROM node_fts") == n1
    finally:
        db.close()


# ── 接进上下文装配 ──────────────────────────────────────────
def test_想起来的进的是_C_段(tmp_path: Path) -> None:
    db, q = _db(tmp_path)
    try:
        rows = recall(db, q.text, actor="her", context=q.context, now_ms=q.ts, k=3)
        seed = PersonaSeed(narrative="我记得住你说过的话。", version=1)
        snap = RelationSnapshot(bucket=0, summary="认识 47 天", hubs=(), phase="醒着")
        bare = assemble(seed, snap, TurnContext(stimulus=q.text))
        full = assemble(seed, snap, TurnContext(stimulus=q.text,
                                               recalled=lines(rows)))
        assert bare.a_hash == full.a_hash        # A 段不变
        assert bare.b == full.b                  # B 段不变
        assert full.c != bare.c                  # 只有 C 段变
        assert "小时前" in full.c or "天前" in full.c
    finally:
        db.close()


# ── 生产路径复现评测质量 ────────────────────────────────────
def test_端到端复现评测质量() -> None:
    r = compare(FS, n_samples=10)
    assert r["prod_r5"] == pytest.approx(1.0), r
    assert r["prod_r1"] >= 0.5, r
