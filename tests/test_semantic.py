"""语义解析：别名表 + 共现推断。**没有向量。**

两个失败模式各测一组：

  别名不展开  → 开会/会议 各自成簇，复现次数永远到不了阈值，漏斗悄悄废掉
  别名乱合并  → 把「相关但不同」的东西并成一个，之后所有概括都串味
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.memory import funnel
from gits.core import project
from gits.memory import semantic
from gits.core.db import DB, Writer
from gits.core.project import entity_id

T0 = 1_800_000_000_000
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _episode(db: DB, nid: str, ts: int, entities: list[str]) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)", (nid, "episode", ts, "world",
                                             3.0, 1.0, ts))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, f"第 {nid} 件事", ts))
        for name in entities:
            eid = entity_id(name)
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            from gits.memory.graph import add_edge

            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)


# ── 别名 ─────────────────────────────────────────────────────


def test_同名同id_异名异id(db) -> None:
    assert entity_id("开会") == entity_id(" 开会 ")
    assert entity_id("开会") != entity_id("会议")


def test_合并之后算一个簇(db) -> None:
    # 间隔 4 天 > s=3，所以本来就该算一次独立重述
    _episode(db, "n:1", T0, ["开会"])
    _episode(db, "n:2", T0 + 4 * DAY, ["会议"])
    before = funnel.cluster_of(db, "开会")
    assert len(before.node_ids) == 1 and before.recurrence == 0, \
        "拆成两个簇时，各只有一件，谈不上复现"

    semantic.merge(db, entity_id("会议"), entity_id("开会"), now=T0)
    got = funnel.cluster_of(db, "开会")
    assert len(got.node_ids) == 2, "开会和会议现在是一个簇"
    assert got.recurrence == 1, "**合并之后才看得见复现**——这正是别名的意义"


def test_别名可以链式(db) -> None:
    semantic.merge(db, entity_id("会议"), entity_id("开会"), now=T0)
    semantic.merge(db, entity_id("那个会"), entity_id("会议"), now=T0 + 1)
    assert semantic.resolve(db, entity_id("那个会")) == entity_id("开会")
    assert semantic.family(db, entity_id("开会")) == {
        entity_id("开会"), entity_id("会议"), entity_id("那个会")}


def test_成环会被拒绝(db) -> None:
    semantic.merge(db, entity_id("会议"), entity_id("开会"), now=T0)
    with pytest.raises(ValueError, match="环"):
        semantic.merge(db, entity_id("开会"), entity_id("会议"), now=T0 + 1)


def test_自己指向自己也拒绝(db) -> None:
    with pytest.raises(ValueError):
        semantic.merge(db, entity_id("开会"), entity_id("开会"), now=T0)


def test_别名走事件_能重放(db) -> None:
    semantic.merge(db, entity_id("会议"), entity_id("开会"), now=T0)
    project.restore(db)
    assert semantic.resolve(db, entity_id("会议")) == entity_id("开会")


# ── 共现推断 ─────────────────────────────────────────────────


def test_共现相似只提议不合并(db) -> None:
    # 每个实体至少出现两次，否则连候选都不该产生（MIN_MENTIONS）。
    # 「开会」和「会议」都和老板、咖啡一起出现 —— 上下文几乎一样。
    _episode(db, "n:1", T0, ["开会", "老板"])
    _episode(db, "n:2", T0 + DAY, ["会议", "老板"])
    _episode(db, "n:3", T0 + 2 * DAY, ["开会", "咖啡"])
    _episode(db, "n:4", T0 + 3 * DAY, ["会议", "咖啡"])
    got = semantic.propose(db)
    pairs = {(a, b) for a, b, _s in got}
    assert (entity_id("开会"), entity_id("会议")) in pairs or \
        (entity_id("会议"), entity_id("开会")) in pairs
    assert semantic.resolve(db, entity_id("会议")) == entity_id("会议"), \
        "只是候选，库里什么都没变"


def test_只出现一次的不参与推断(db) -> None:
    _episode(db, "n:1", T0, ["开会", "老板"])
    assert semantic.propose(db) == []


def test_已经是同一个就不必再提(db) -> None:
    _episode(db, "n:1", T0, ["开会", "老板"])
    _episode(db, "n:2", T0 + DAY, ["会议", "老板"])
    semantic.merge(db, entity_id("会议"), entity_id("开会"), now=T0)
    pairs = {(a, b) for a, b, _s in semantic.propose(db)}
    assert (entity_id("开会"), entity_id("会议")) not in pairs


def test_共现门槛能调(db) -> None:
    _episode(db, "n:1", T0, ["开会", "老板", "咖啡"])
    _episode(db, "n:2", T0 + DAY, ["会议", "老板", "咖啡"])
    assert semantic.propose(db, threshold=0.99) == []
    assert semantic.propose(db, threshold=0.5) != []
