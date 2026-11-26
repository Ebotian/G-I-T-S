"""上下文扩展 + 用 r 区分「哪一条」。

这一层是这轮的重点：同一实体的多条事实字面完全一样，
**相似度在这里没有信息**，能区分它们的只有「哪条还活着、哪条还记得住」。
"""

from __future__ import annotations

import sqlite3

import pytest

from gits.memory.search import index_node, search


def _c() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_只有上下文能召回() -> None:
    """裸问一个字都不重叠；补上刚聊过的内容就能命中。

    这不是检索技巧，是指代本来就这么解析的。
    """
    c = _c()
    index_node(c, "f1", "物07 状态 卡住了")
    index_node(c, "f2", "物09 计划 太贵")

    assert search(c, "上次那个跟你较劲的事", k=5) == []

    hits = search(c, "上次那个跟你较劲的事", k=5, context="物07 又卡住了")
    assert hits and hits[0].node_id == "f1"


def test_上下文不会污染无关节点() -> None:
    c = _c()
    index_node(c, "f1", "物07 状态 卡住了")
    index_node(c, "f2", "物09 计划 太贵")
    hits = search(c, "那个事", k=5, context="太贵")
    assert [h.node_id for h in hits] == ["f2"]


def test_命中的文档相似度不会是零() -> None:
    """两篇文档里都出现的词 IDF≈0，bm25 可能是 0——但那条确实命中了。
    映射成 0 会让它和「没命中」无法区分，排序退化成插入顺序。"""
    c = _c()
    index_node(c, "a", "物07 状态")
    index_node(c, "b", "物08 状态")
    hits = search(c, "状态", k=5)
    assert len(hits) == 2
    assert all(h.sim > 0 for h in hits)


def test_同一实体多条时_r_决定选哪一条() -> None:
    """字面完全一样 → sim 一样 → 只剩 r 能区分。"""
    c = _c()
    for nid in ("f1", "f2", "f3"):
        index_node(c, nid, "物07 状态 卡住了")

    def r_of(nid: str) -> float:
        return {"f1": 0.1, "f2": 0.9, "f3": 0.3}[nid]

    ranked = [h.node_id for h in search(c, "物07 状态", k=3, r_of=r_of)]
    assert ranked == ["f2", "f3", "f1"]


def test_已过期的版本沉底() -> None:
    """版本链：被取代的旧版本 r 置 0，就不该排在前面。"""
    c = _c()
    index_node(c, "old", "物07 偏好 想换掉")
    index_node(c, "new", "物07 偏好 其实还行")

    def r_of(nid: str) -> float:
        return 0.0 if nid == "old" else 0.8

    hits = search(c, "物07 偏好", k=5, r_of=r_of)
    assert hits[0].node_id == "new"


def test_上下文为空时行为不变() -> None:
    c = _c()
    index_node(c, "f1", "物07 状态 卡住了")
    a = [h.node_id for h in search(c, "状态 卡住", k=3)]
    b = [h.node_id for h in search(c, "状态 卡住", k=3, context="")]
    assert a == b
