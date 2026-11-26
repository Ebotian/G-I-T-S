"""主动开口的执行路径：决定 → 入队 → 才算说过。

四个时刻必须分开，混在一起就会"为一句他没收到的话罚他"。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory.graph import add_edge
from gits.speech import initiative as I
from gits.speech import opening
from gits.speech.llm import Reply

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    # A 段不能是空的：没有自我就不说话（NoPersona），主动开口也一样。
    from gits.core.project import restore
    from gits.memory.persona import seed

    seed(d, "我不太会哄人。", now=T0)
    restore(d)
    yield d
    d.close()


def _dangling(db: DB, name: str = "看病") -> str:
    from gits.core.project import entity_id

    eid = entity_id(name)
    for i in range(3):
        ts = T0 - 6 * HOUR + i * HOUR
        nid = f"n:{name}-{i}"
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r,"
                      " last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "episode", ts, "world", 1.0, 1.0, ts))
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)
    return eid


def test_刺激源给意图不给句子() -> None:
    """写成一整句就是脚本化措辞——和她该自己决定说什么相冲突。"""
    got = opening.stimulus("看病")
    assert "看病" in got
    assert "问" not in got and "说" not in got, f"替她决定了怎么说：{got}"


def test_说完才入队才算说过(db, monkeypatch) -> None:
    from gits.speech import opening as O

    _dangling(db)
    ev = I.record(db, now=T0, reason="决定要说", candidate="看病")
    assert I.spoken(db) == [], "还没发出去"

    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="在吗？", model="stub", usage={}, latency_ms=1))
    out = O.compose(db, initiative_event=ev, candidate="看病",
                    channel="weixin", now=T0, model="stub")
    assert out is not None
    assert I.spoken(db) == [T0], "发出去了才算说过"


def test_没说出话就不入队(db, monkeypatch) -> None:
    """模型交了空——那不是"说过"，压力不该降。"""
    from gits.speech import opening as O

    _dangling(db)
    ev = I.record(db, now=T0, reason="决定", candidate="看病")
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="   ", model="stub", usage={}, latency_ms=1))
    assert O.compose(db, initiative_event=ev, candidate="看病",
                     channel="weixin", now=T0, model="stub") is None
    assert db.scalar("SELECT count(*) FROM outbox") == 0
    assert I.spoken(db) == []


def test_入队的消息指向那条决定(db, monkeypatch) -> None:
    """trace 要能一路追到"她为什么开口"。"""
    from gits.speech import opening as O

    _dangling(db)
    ev = I.record(db, now=T0, reason="决定", candidate="看病")
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="在吗？", model="stub", usage={}, latency_ms=1))
    O.compose(db, initiative_event=ev, candidate="看病", channel="weixin",
              now=T0, model="stub")
    row = db.one("SELECT ref_event_id, status FROM outbox")
    assert int(row["ref_event_id"]) == ev
    assert row["status"] == "pending"


def test_生成的那条花的是这次决定的成本(db, monkeypatch) -> None:
    from gits.speech import opening as O

    _dangling(db)
    ev = I.record(db, now=T0, reason="决定", candidate="看病")
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="在吗？", model="stub", usage={}, latency_ms=1))
    O.compose(db, initiative_event=ev, candidate="看病", channel="weixin",
              now=T0, model="stub")
    kinds = [r["kind"] for r in db.q("SELECT kind FROM model_calls")]
    assert "opening" in kinds, kinds


def test_开口之后反刍次数会增加(db, monkeypatch) -> None:
    """"同一件事问过多次就翻篇"——这条机制以前在线上从来没生效过，
    因为只有测试调 record_use()，真实路径不记。"""
    from gits.core.project import entity_id
    from gits.speech import opening as O

    eid = _dangling(db)
    assert db.scalar("SELECT strikes FROM rumination WHERE entity=?", (eid,)) is None
    ev = I.record(db, now=T0, reason="决定", candidate="看病")
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="在吗？", model="stub", usage={}, latency_ms=1))
    O.compose(db, initiative_event=ev, candidate="看病", channel="weixin",
              now=T0, model="stub")
    got = db.scalar("SELECT strikes FROM rumination WHERE entity=?", (eid,))
    assert got == 1, f"开口之后没记反刍次数：{got}"


def test_没发出去就不记反刍(db, monkeypatch) -> None:
    """决定了但没发出去，不算"问过"。"""
    from gits.speech import opening as O

    eid = _dangling(db)
    ev = I.record(db, now=T0, reason="决定", candidate="看病")
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="", model="stub", usage={}, latency_ms=1))
    O.compose(db, initiative_event=ev, candidate="看病", channel="weixin",
              now=T0, model="stub")
    assert db.scalar("SELECT strikes FROM rumination WHERE entity=?",
                     (eid,)) is None


def test_主动开口从她的额度里扣(db, monkeypatch) -> None:
    """这是她想去说，不是他先开口——钱从她自己的 allowance 出。"""
    from gits.io import ledger

    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="在吗？", model="stub",
                              usage={"prompt_tokens": 900,
                                     "completion_tokens": 100},
                              latency_ms=1))
    before = ledger.state(db, actor="her", now=T0)["spent_today"]
    opening.compose(db, initiative_event=1, candidate=None,
                    channel="weixin", to={"to": "u1"},
                    model="stub", now=T0)
    after = ledger.state(db, actor="her", now=T0)["spent_today"]
    assert after > before, "主动开口没记账"
    raw = db.q("SELECT payload FROM events WHERE kind='spend'")
    assert any(b"opening" in bytes(r["payload"]) for r in raw)
