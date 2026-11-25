"""图上的统计沉淀：带遗忘的计数 + 行归一化的概率。

四条规矩，每条都对应一种具体的错法：

  **衰减**      纯 w = w + 1 会让「三年前」和「昨天」一样重
  **重放确定**  把 now 写进存储，同一个日志重放两次会得到不同的图
  **成员资格**  行存在就永久；跟着 w 归零会让簇静默消失（漏斗再也不压它）
  **按强度算**  数行数 vs 求和 w，是 10 次深谈和 10 次顺嘴一提的区别
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from gits import graph
from gits.db import DB, Writer
from gits.project import entity_id

T0 = 1_800_000_000_000
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _link(db: DB, src: str, dst: str, ts: int, w: float = 1.0) -> None:
    with Writer(db).txn() as c:
        graph.add_edge(c, src=src, dst=dst, kind="mentions", ts=ts, w=w)


def test_三年前的不该和昨天一样重(db) -> None:
    _link(db, "n:old", "e:x", T0)
    _link(db, "n:new", "e:x", T0 + 365 * DAY)
    got = graph.sources_of(db, "e:x", now=T0 + 365 * DAY)
    probs = dict(got)
    assert probs["n:new"] > probs["n:old"] * 3, "衰减没生效"


def test_概率行和是一(db) -> None:
    for i in range(5):
        _link(db, f"n:{i}", "e:x", T0 + i * DAY)
    got = graph.sources_of(db, "e:x", now=T0 + 10 * DAY)
    assert len(got) == 5
    assert sum(p for _d, p in got) == pytest.approx(1.0)


def test_重放是确定的(db, tmp_path: Path) -> None:
    """"""
    from gits import project

    ev, _ = Writer(db).append_event(
        source="shell", kind="msg_in", ext_id="m1", ts=T0, recv_ts=T0,
        payload="提到开会".encode())
    Writer(db).append_model_call(
        kind="extract", model="stub", event_id=ev,
        response='{"summary":"他提到开会",'
                 '"entities":[{"name":"开会","kind":"topic"}],"claims":[]}')
    project.restore(db)
    first = db.scalar("SELECT w FROM edges WHERE kind='mentions'")
    project.restore(db)
    second = db.scalar("SELECT w FROM edges WHERE kind='mentions'")
    assert first == second == 1.0, "同一个日志重放两次，权重必须一致"


def test_成员资格不跟着权重归零(db) -> None:
    _link(db, "n:a", "e:x", T0)
    much_later = T0 + 3650 * DAY          # 十年后，强度几乎为零
    assert graph.effective(1.0, T0, much_later) < 1e-40
    # 但这一行还在——漏斗按行取簇，不能因为"弱"就当它不存在
    assert db.scalar("SELECT count(*) FROM edges") == 1


def test_强度是求和不是数行数(db) -> None:
    _link(db, "n:deep", "e:x", T0, w=10.0)
    _link(db, "n:casual", "e:x", T0 + DAY, w=1.0)
    probs = dict(graph.sources_of(db, "e:x", now=T0 + DAY))
    assert probs["n:deep"] > probs["n:casual"], "深谈该比顺嘴一提重"


def test_注意力按分量算而不是按条数(db) -> None:
    from gits import attention

    # 同样 3 条边，但一边每条都很重；实体节点要存在，_mentions 要 join 名字
    for name in ("开会", "咖啡"):
        eid = entity_id(name)
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope) VALUES(?,?,?,?)",
                      (eid, "entity", T0, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, T0))
    for i in range(3):
        _link(db, f"n:heavy{i}", entity_id("开会"), T0 + i * DAY, w=8.0)
    for i in range(3):
        _link(db, f"n:light{i}", entity_id("咖啡"), T0 + i * DAY, w=1.0)
    got = attention._mentions(db, now=T0 + 3 * DAY)
    assert got["开会"] > got["咖啡"] * 3
