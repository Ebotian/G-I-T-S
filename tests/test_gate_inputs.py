"""开口决策 + 悬着的事 + 压力。

三条要盯的：

  没有时钟规则   承诺线已删。凌晨不该说话，靠"那时候线索早凉了"自然成立
  不会饿死       压力随等待时间上升，门槛随之下降；否则主动行为永远不触发
  可解释        每个决策带 reason，包括"憋了多少"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.speech import gate
from gits.speech import unresolved
from gits.core.db import DB, Writer
from gits.memory.graph import add_edge
from gits.core.project import entity_id

T0 = 1_800_000_000_000
HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _episode(db: DB, nid: str, ts: int, entities: list[str]) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)", (nid, "episode", ts, "world", 3.0, 1.0, ts))
        for name in entities:
            eid = entity_id(name)
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)


def _claim(db: DB, ts: int, subject: str, obj: str) -> None:
    with Writer(db).txn() as c:
        add_edge(c, src=entity_id(subject), dst=entity_id(obj),
                 kind="rel:在意", ts=ts, w=1.0)


# ── 未解释度 / 可解释度 ─────────────────────────────────────


def test_被反复提到却一无所知_未解释度高(db) -> None:
    for i in range(3):
        _episode(db, f"n:{i}", T0 + i * DAY, ["那个 PR"])
    got = unresolved.scan(db, now=T0 + 2 * DAY)
    assert got and got[0].entity == entity_id("那个 PR")
    assert got[0].unexcluded == pytest.approx(1.0)
    assert got[0].known == 0


def test_知道得越多越没什么可问(db) -> None:
    for i in range(3):
        _episode(db, f"n:{i}", T0 + i * DAY, ["那个 PR"])
    for i in range(3):
        _claim(db, T0 + i, "那个 PR", f"东西{i}")
    got = unresolved.scan(db, now=T0 + 2 * DAY)
    # unexcluded 归零之后它就不该出现在候选里了——没什么可问的
    assert got == [] or got[0].unexcluded == pytest.approx(0.0)


def test_冷了的线索可解释度低(db) -> None:
    fresh = DB(":memory:")
    for i in range(3):
        _episode(db, f"n:{i}", T0 + i * DAY, ["那个 PR"])
    hot = unresolved.scan(db, now=T0 + 3 * DAY)[0]
    cold = unresolved.scan(db, now=T0 + 300 * DAY)[0]
    assert cold.resolvable < hot.resolvable / 5, \
        "半年没提过的线索，问了也白问——这就是「凌晨不该冒话」的机制，不是规则"
    fresh.close()


def test_只提过一次的不进候选(db) -> None:
    _episode(db, "n:0", T0, ["一嘴带过"])
    assert unresolved.scan(db, now=T0) == []


# ── 压力 ─────────────────────────────────────────────────────


def test_压力随时间上升且有界(db) -> None:
    assert unresolved.pressure(0) == 0.0
    assert 0.5 < unresolved.pressure(36 * HOUR) < 0.7
    assert unresolved.pressure(1000 * HOUR) < 1.0, "永远逼近 1，不到 1"


def test_压力会把门槛压低(db) -> None:
    assert gate.threshold_for(0.0) == pytest.approx(gate.THRESHOLD)
    assert gate.threshold_for(1.0) == pytest.approx(
        gate.THRESHOLD * (1 - gate.MAX_DROP))
    assert gate.threshold_for(5.0) == gate.threshold_for(1.0), "夹在 0..1"


def test_憋久了价值不够也能开口(db) -> None:
    i = gate.GateInputs(unexcluded=0.6, resolvable=0.4, reciprocity=1.0)
    # 价值 0.24：本来过不了 0.15？过得了。换个更低的
    low = gate.GateInputs(unexcluded=0.25, resolvable=0.3, reciprocity=1.0)
    assert not gate.evaluate(low).allow, "价值 0.075 < 0.15"
    assert gate.evaluate(low, pressure=1.0).allow, "憋久了门槛降到 0.06，就该说"


def test_从来没有主动开口过_压力拉满(db) -> None:
    assert unresolved.since_last_open(db, now=T0) is None
    _i, p, _best = unresolved.inputs(db, now=T0)
    assert p == 1.0, "第一次开口不该因为「没历史」而永远发不出去"


def test_主动开口之后压力从头算(db) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,'在吗','sent',NULL)", (T0, "weixin"))
    assert unresolved.since_last_open(db, now=T0 + HOUR) == HOUR
    _i, p, _b = unresolved.inputs(db, now=T0 + HOUR)
    assert p < 0.05, "刚说过话，没有压力"


def test_回应不算主动开口(db) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,'嗯','sent',7)", (T0, "weixin"))
    assert unresolved.since_last_open(db, now=T0 + HOUR) is None, \
        "回应是别人引起的，不该清空「她多久没主动说过话」"


# ── 没有时钟规则 ─────────────────────────────────────────────


def test_承诺线已经删掉了(db) -> None:
    assert not hasattr(gate.GateInputs(), "promise_violated")
    import inspect

    src = inspect.getsource(gate)
    assert "promise_violated" not in src, "门里不该再有「某个时段不许说话」这种规则"
