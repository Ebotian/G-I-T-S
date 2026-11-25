"""检索：分词、索引、排序方向、打分。

有一条测试是专门为"我今天写反的那个方向"留的：
bm25 越负越好，相似度必须随 |rank| **增大**。写反了不会报错，
只会把结果全排反——而且看起来"能跑"。
"""

from __future__ import annotations

import sqlite3

import pytest

from gits.search import (
    Weights, bm25_to_sim, combine, index_node, search, tokenize,
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


# ── 分词 ────────────────────────────────────────────────────
def test_中文按二元组() -> None:
    assert tokenize("卡住了") == "卡住 住了"


def test_单字保留() -> None:
    assert tokenize("猫") == "猫"


def test_拉丁按词且小写() -> None:
    assert tokenize("Rust Borrow_Checker") == "rust borrow_checker"


def test_中英混排都能切() -> None:
    toks = tokenize("用 Rust 写了个 解析器")
    assert "rust" in toks and "解析" in toks and "析器" in toks


def test_空串不炸() -> None:
    assert tokenize("") == ""


# ── 排序方向（就是那个 bug）─────────────────────────────────
def test_bm25_越负相似度越高() -> None:
    assert bm25_to_sim(-8.0) > bm25_to_sim(-2.0) > bm25_to_sim(-0.1)
    assert 0 <= bm25_to_sim(-50.0) < 1


def test_bm25_零分是零() -> None:
    assert bm25_to_sim(0.0) == 0.0


# ── 索引与检索 ──────────────────────────────────────────────
def test_字面命中排第一() -> None:
    c = _conn()
    index_node(c, "a", "物07 状态 卡住了")
    index_node(c, "b", "物08 计划 太贵")
    hits = search(c, "状态 卡住了", k=5)
    assert hits and hits[0].node_id == "a"


def test_无关查询返回空() -> None:
    c = _conn()
    index_node(c, "a", "物07 状态 卡住了")
    assert search(c, "量子色动力学", k=5) == []


def test_重复索引不会产生重复行() -> None:
    c = _conn()
    index_node(c, "a", "卡住了")
    index_node(c, "a", "卡住了")
    assert c.execute("SELECT count(*) FROM node_fts").fetchone()[0] == 1


def test_改内容会替换旧索引() -> None:
    c = _conn()
    index_node(c, "a", "卡住了")
    index_node(c, "a", "太贵")
    assert search(c, "卡住了", k=3) == []
    assert search(c, "太贵", k=3)[0].node_id == "a"


# ── 打分 ────────────────────────────────────────────────────
def test_线性权重() -> None:
    w = Weights(sim=1.0, r=0.0, energy=0.0, centrality=0.0, recency=0.0)
    assert combine(sim=0.5, w=w) == pytest.approx(0.5)
    w2 = Weights(sim=0.0, r=1.0, energy=0.0, centrality=0.0, recency=0.0)
    assert combine(r=0.25, w=w2) == pytest.approx(0.25)


def test_r_会改变排序() -> None:
    c = _conn()
    index_node(c, "a", "状态 卡住")
    index_node(c, "b", "状态 卡住")
    base = [h.node_id for h in search(c, "状态 卡住", k=5)]
    assert set(base) == {"a", "b"}

    def r_of(nid: str) -> float:
        return 1.0 if nid == "b" else 0.0

    ranked = [h.node_id for h in search(c, "状态 卡住", k=5, r_of=r_of)]
    assert ranked[0] == "b"


def test_向量是另一条召回通道_不是重排钩子() -> None:
    """词面完全没命中的节点，必须能被向量召回来。

    只做重排的话，"间接提问"永远救不回来——它压根不在候选里。
    """
    c = _conn()
    index_node(c, "a", "物07 状态 卡住了")
    index_node(c, "b", "物09 计划 太贵")
    hits = search(c, "那个跟你较劲的事", k=5,
                  candidates_of=lambda q: [("b", 1.0)])
    assert hits and hits[0].node_id == "b"
    assert "vec" in hits[0].why


def test_两条通道取较高的相似度() -> None:
    """同一节点被两条通道都召回时，取高的那个；低的不会把它压下去。"""
    c = _conn()
    index_node(c, "a", "状态 卡住")
    index_node(c, "b", "计划 太贵")

    h_low = search(c, "状态 卡住", k=5, candidates_of=lambda q: [("a", 0.01)])[0]
    assert h_low.sim >= 0.01

    h_high = search(c, "状态 卡住", k=5, candidates_of=lambda q: [("a", 1.0)])[0]
    assert h_high.sim == pytest.approx(1.0)
    assert "vec" in h_high.why
