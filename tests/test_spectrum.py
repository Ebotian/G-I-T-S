"""频谱式上下文填充。

盯三个错法：

  同一频带被填满   十个浅记忆把名额占光，一条概括都进不去
  不正交           摘要和它的原件同时出现，同一件事说两遍
  倾斜反了         激动的时候她开始讲一贯的样子，平静的时候讲细节
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory.recall import Recalled, recall, reindex
from gits.memory.spectrum import allocate, band_of

T0 = 1_800_000_000_000
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _node(db: DB, nid: str, text: str, *, s: float, ts: int = T0) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)", (nid, "episode", ts, "world", s, 1.0, ts))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, text, ts))


def _hit(nid: str, r: float = 1.0, score: float | None = None) -> Recalled:
    return Recalled(node_id=nid, score=r if score is None else score,
                    sim=0.5, r=r, text=nid, age_ms=0)


def test_频带由稳定期读出来(db) -> None:
    assert band_of(0.5) == band_of(1.0) == 0, "不到一天都是这一轮的事"
    assert band_of(2.0) == 1 and band_of(30.0) == 4


def test_十个浅的不该把名额占光(db) -> None:
    for i in range(10):
        _node(db, f"n:s{i}", f"今天的琐事{i}", s=1.0)
    for i in range(2):
        _node(db, f"n:d{i}", f"长期的样子{i}", s=30.0)
    hits = [_hit(f"n:s{i}") for i in range(10)] + [_hit(f"n:d{i}") for i in range(2)]
    got = allocate(db, hits, slots=5)
    ids = [h.node_id for h in got]
    assert any(i.startswith("n:d") for i in ids), "低频分量不能被挤光"
    assert len(ids) == 5


def test_先概括后细节(db) -> None:
    _node(db, "n:deep", "长期的样子", s=30.0)
    _node(db, "n:shallow", "今天的一件事", s=1.0)
    got = allocate(db, [_hit("n:shallow"), _hit("n:deep")], slots=2)
    assert [h.node_id for h in got] == ["n:deep", "n:shallow"], \
        "写法上先给一贯的样子，再给这一件事"


def test_被吸收的原件不再单列(db) -> None:
    _node(db, "n:abs", "他总在会上较真", s=30.0)
    _node(db, "n:raw", "他今天又在会上较真了", s=1.0)
    with Writer(db).txn() as c:
        c.execute("UPDATE node_content SET absorbed_into=? WHERE node_id=?",
                  ("n:abs", "n:raw"))
    got = allocate(db, [_hit("n:abs"), _hit("n:raw")], slots=5)
    assert [h.node_id for h in got] == ["n:abs"], "同一件事不说两遍"


def test_吸收对象不在候选里时原件照常出现(db) -> None:
    _node(db, "n:raw", "他今天又在会上较真了", s=1.0)
    with Writer(db).txn() as c:
        c.execute("UPDATE node_content SET absorbed_into=? WHERE node_id=?",
                  ("n:gone", "n:raw"))
    got = allocate(db, [_hit("n:raw")], slots=5)
    assert [h.node_id for h in got] == ["n:raw"], "摘要没被召回到，就别删原件"


def test_激动时抬高频(db) -> None:
    for i in range(5):
        _node(db, f"n:s{i}", f"今天的细节{i}", s=1.0)
    for i in range(5):
        _node(db, f"n:d{i}", f"长期的样子{i}", s=32.0)
    hits = [_hit(f"n:s{i}", 0.6) for i in range(5)] + [_hit(f"n:d{i}", 0.6) for i in range(5)]
    calm = [h.node_id for h in allocate(db, hits, slots=4, arousal=0.0)]
    excited = [h.node_id for h in allocate(db, hits, slots=4, arousal=1.0)]
    assert sum(1 for i in excited if i.startswith("n:s")) > \
        sum(1 for i in calm if i.startswith("n:s")), "激动时讲具体那件事"


def test_平静时抬低频(db) -> None:
    for i in range(5):
        _node(db, f"n:s{i}", f"今天的细节{i}", s=1.0)
    for i in range(5):
        _node(db, f"n:d{i}", f"长期的样子{i}", s=32.0)
    hits = [_hit(f"n:s{i}", 0.6) for i in range(5)] + [_hit(f"n:d{i}", 0.6) for i in range(5)]
    calm = [h.node_id for h in allocate(db, hits, slots=4, arousal=0.0)]
    assert sum(1 for i in calm if i.startswith("n:d")) > \
        sum(1 for i in calm if i.startswith("n:s")), "平静时讲一贯的样子"


def test_倾斜可以关掉(db) -> None:
    for i in range(4):
        _node(db, f"n:s{i}", f"细节{i}", s=1.0)
    for i in range(4):
        _node(db, f"n:d{i}", f"概括{i}", s=32.0)
    hits = [_hit(f"n:s{i}", 0.5) for i in range(4)] + [_hit(f"n:d{i}", 0.5) for i in range(4)]
    a = [h.node_id for h in allocate(db, hits, slots=4, arousal=1.0, tilt=0.0)]
    b = [h.node_id for h in allocate(db, hits, slots=4, arousal=0.0, tilt=0.0)]
    assert a == b, "tilt=0 时情绪不该影响分配"


def test_空输入不炸(db) -> None:
    assert allocate(db, [], slots=5) == []
    assert allocate(db, [_hit("n:x")], slots=0) == []


def test_名额比带数还少时按证据量(db) -> None:
    _node(db, "n:a", "a", s=1.0)
    _node(db, "n:b", "b", s=8.0)
    _node(db, "n:c", "c", s=64.0)
    got = allocate(db, [_hit("n:a", 0.1), _hit("n:b", 0.2), _hit("n:c", 0.9)],
                    slots=1)
    assert [h.node_id for h in got] == ["n:c"]


def test_带内按相关性排_不是按稳定期(db) -> None:
    """只按 r 排的话，一条新近纠正、精准命中问题的记忆会输给
    一条熟悉但关系较弱的旧记忆——而「记得牢」不等于「答得上」。
    """
    _node(db, "n:familiar", "熟悉的旧事", s=8.0)
    _node(db, "n:fresh", "刚纠正的那件事", s=8.0)
    hits = [_hit("n:familiar", r=0.95, score=0.30),
            _hit("n:fresh", r=0.40, score=0.90)]
    got = allocate(db, hits, slots=2)
    assert [h.node_id for h in got] == ["n:fresh", "n:familiar"]


def test_频带名额按相关性分_不是按稳定期(db) -> None:
    """用 r 分名额的话，一批"被反复选中所以记得牢"的旧记忆会长期霸占，
    而那是系统自己的选材偏好被当成了证据。
    """
    # 高频带：很多条、r 低但相关性高；低频带：r 高但相关性低
    for i in range(4):
        _node(db, f"n:f{i}", f"刚发生的事{i}", s=1.0)
        _node(db, f"n:o{i}", f"很久以前的事{i}", s=64.0)
    hits = ([_hit(f"n:f{i}", r=0.2, score=0.9) for i in range(4)]
            + [_hit(f"n:o{i}", r=0.99, score=0.05) for i in range(4)])
    got = [h.node_id for h in allocate(db, hits, slots=4, arousal=0.5,
                                      tilt=0.0)]
    fresh = sum(1 for i in got if i.startswith("n:f"))
    assert fresh > 2, f"相关性高的频带只拿到 {fresh}/4 个名额"
