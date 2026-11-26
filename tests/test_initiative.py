"""主动开口：压力按"闲的时间"积累，惩罚按"该回却没回"加权。

四件事各有测试，每件对应一个具体的错法：
  压力用墙钟        → 他在开会和他在闲着，被当成同一件事
  惩罚不看在线概率   → 沉默被当成统一的负面信号
  不允许重发        → 一句话石沉大海就再也不说了
  到处采样          → 同一分钟内问两次得到不同答案，日志对不上
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory.graph import add_edge
from gits.speech import initiative as I

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _dangling(db: DB, name: str = "看病", *, times: int = 3,
              start: int = T0 - 6 * HOUR) -> str:
    from gits.core.project import entity_id

    eid = entity_id(name)
    for i in range(times):
        ts = start + i * HOUR
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


def _busy(db: DB, hours=(14, 15, 16, 17)) -> None:
    """他只在 14-17 点说话。**每条都标已回**，免得变成积压。"""
    with Writer(db).txn() as c:
        for d in range(7):
            for h in hours:
                ts = T0 - d * 86_400_000 + (h - 15) * HOUR
                cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind,"
                                " ext_id, payload, lag, ts_trust, scope)"
                                " VALUES(?,?,?,?,?,?,?,?,?)",
                                (ts, ts, "shell", "msg_in", f"m{d}-{h}",
                                 b"hi", 0, 1, "world"))
                c.execute("INSERT INTO events(ts, recv_ts, source, kind,"
                          " ext_id, ts_trust, scope) VALUES(?,?,?,?,?,?,?)",
                          (ts, ts, "ghost", "replied",
                           f"replied:{cur.lastrowid}", 1, "world"))


def test_压力积的是闲的时间不是墙钟(db) -> None:
    """同样隔 3 小时，他在闲的时候攒得快，在忙的时候攒得慢。"""
    _busy(db, hours=(14, 15, 16, 17))
    morning = int(dt.datetime(2026, 11, 26, 10, 0).timestamp() * 1000)
    I.record(db, now=morning - 3 * HOUR, reason="x", candidate=None)
    idle_hours = I.pressure(db, now=morning)          # 07-10 点：他基本不说话
    I.record(db, now=T0, reason="x", candidate=None)
    busy_hours = I.pressure(db, now=T0 + 3 * HOUR)    # 15-18 点：他在说话
    assert busy_hours > idle_hours, (idle_hours, busy_hours)


def test_没回复的惩罚按当时在线概率加权(db) -> None:
    """他在线却没回 → 罚得重；他本来就不在 → 罚得轻。"""
    _busy(db)
    # 03:00：他基本不在 → 沉默在预期里
    quiet = int(dt.datetime(2026, 11, 26, 3, 0).timestamp() * 1000)
    # 17:30：他在说话，而且之后六小时没有他的消息 → 沉默是意外
    busy = int(dt.datetime(2026, 11, 26, 17, 30).timestamp() * 1000)
    I.record(db, now=quiet, reason="夜里说的", candidate=None)
    I.record(db, now=busy, reason="白天说的", candidate=None)
    factor, weight = I.unanswered(db, now=T0 + 10 * HOUR)
    assert weight > 0
    assert factor < 1.0, "白天的沉默没被罚"
    # 只看夜里那一次：他基本不在，沉默完全在预期里 → 不该罚
    only_night = I.unanswered(db, now=quiet + 8 * HOUR)[0]
    assert only_night == pytest.approx(1.0), only_night
    assert only_night > factor, "夜里的沉默不该和白天罚一样重"


def test_允许重发但惩罚累积(db) -> None:
    """一句话石沉大海就再也不说了，那是另一种坏。"""
    _busy(db)
    # 都挑 17:30：那时他在说话，之后六小时没有他的消息 → 每次都算欠
    d1 = int(dt.datetime(2026, 11, 24, 17, 30).timestamp() * 1000)
    d2 = int(dt.datetime(2026, 11, 25, 17, 30).timestamp() * 1000)
    d3 = int(dt.datetime(2026, 11, 26, 17, 30).timestamp() * 1000)
    one = I.unanswered(db, now=d1)[0]
    I.record(db, now=d1, reason="1", candidate=None)
    two = I.unanswered(db, now=d2)[0]
    I.record(db, now=d2, reason="2", candidate=None)
    three = I.unanswered(db, now=d3)[0]
    assert three < two < one, (one, two, three)
    assert three > 0, "罚到零就永远不说了——那是饿死"


def test_采样不固定种子_但围绕期望值(db) -> None:
    """**不固定种子。** 决定会落成事件，重放读的是记下的结果，不是重抽一次。
    要保证的只有：抽出来的数围绕期望值，而且真的在抽。"""
    xs = [I.sample_value(0.4) for _ in range(300)]
    mean = sum(xs) / len(xs)
    assert 0.3 < mean < 0.5, mean
    assert len({round(x, 4) for x in xs}) > 200, "采样退化成常数了"


def test_决策用的是采样值不是期望值(db) -> None:
    """**只在最终决策时取一次值。** 之前全是分布，这里是一次抽样。"""
    _dangling(db)
    _busy(db)
    vals = []
    for i in range(30):
        got = I.check(db, now=T0 + i * 60_000)
        vals.append((got["value"], got["expected"]))
    assert any(abs(v - e) > 1e-6 for v, e in vals), \
        "value 和 expected 永远相等 —— 说明没采样，直接用了期望值"


def test_采样围绕期望值抖(db) -> None:
    xs = [I.sample_value(0.5, actor="her", now=T0 + i * 60_000)
          for i in range(200)]
    mean = sum(xs) / len(xs)
    assert 0.35 < mean < 0.65, mean
    assert len(set(round(x, 4) for x in xs)) > 100, "采样退化成常数了"


def test_没有悬着的事就不说(db) -> None:
    _busy(db)
    got = I.check(db, now=T0)
    assert got["allow"] is False
    assert "悬着" in got["why"]


def test_憋久了就该开口(db) -> None:
    _dangling(db)
    _busy(db)
    got = I.check(db, now=T0)
    assert got["pressure"] == pytest.approx(1.0)
    assert got["allow"] is True, got["why"]


def test_出口断了就不开口(db) -> None:
    _dangling(db)
    _busy(db)
    got = I.check(db, now=T0, channel_online=False)
    assert got["allow"] is False
    assert "出口" in got["why"]


# ── 欠账和压力是同一个积分 ──────────────────────────────────


def test_欠账也是积分出来的(db) -> None:
    """**同一种量，不同窗口。** 说得通才不是两套逻辑各算各的。"""
    _busy(db)
    # 17:30 说了一句：他在 17 点那个小时是活跃的，但之后到第二天都没有消息
    said = int(dt.datetime(2026, 11, 26, 17, 30).timestamp() * 1000)
    I.record(db, now=said, reason="x", candidate=None)
    idle = I.opportunity(db, start=said, end=said + 6 * HOUR)
    assert idle > 0.2, idle
    factor, weight = I.unanswered(db, now=said + 8 * HOUR)
    assert weight > 0, "他在线却没回，应该欠一笔"
    assert factor < 1.0, "欠了却没罚"
    # 罚的分量来自同一个积分，不是另算一个数
    assert weight == pytest.approx(min(1.0, idle / I.REPLY_HORIZON_HOURS))


def test_他没闲过就不算欠(db) -> None:
    """他整段时间都不在线，沉默完全在预期里 —— 一笔也不欠。"""
    _busy(db, hours=(15,))
    night = int(dt.datetime(2026, 11, 26, 1, 0).timestamp() * 1000)
    I.record(db, now=night, reason="x", candidate=None)
    idle = I.opportunity(db, start=night, end=night + 6 * HOUR)
    assert idle == pytest.approx(0.0), idle
    factor, weight = I.unanswered(db, now=night + 8 * HOUR)
    assert weight == pytest.approx(0.0)
    assert factor == pytest.approx(1.0), "不该罚"


def test_闲得越久欠得越多(db) -> None:
    """欠账随「他闲了多久」连续增长，不是 0/1 跳变。"""
    _busy(db, hours=(15,))
    base = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
    I.record(db, now=base, reason="x", candidate=None)
    soon = I.unanswered(db, now=base + 30 * 60_000)[0]
    later = I.unanswered(db, now=base + 5 * HOUR)[0]
    assert later < soon < 1.0, (soon, later)


def test_欠账要归一化_一直在也不该无限罚(db, monkeypatch) -> None:
    """他整六小时都在线却没回，和他在线一小时没回，罚应该一样重——
    **归一化到 REPLY_HORIZON，不是越长欠越多。** 否则久了会把话说死。"""
    _busy(db)
    monkeypatch.setattr(I, "online", lambda db, ts, snap=None: 1.0)
    said = int(dt.datetime(2026, 11, 26, 1, 0).timestamp() * 1000)
    I.record(db, now=said, reason="x", candidate=None)
    _factor, weight = I.unanswered(db, now=said + 8 * HOUR)
    assert weight == pytest.approx(1.0), weight
