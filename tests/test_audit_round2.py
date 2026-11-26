"""第二轮评审指出的问题：每条都是"结论跨模块变了含义"。

验收标准是**反例**，不是"看起来对"：
  决定说  ≠ 说出去
  他回没回 ≠ 窗口里有没有消息
  上次确认在那 ≠ 现在还在那
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory.graph import add_edge
from gits.speech import initiative as I
from gits.world import brief as B
from gits.world import loc, places

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
HOUR = 3_600_000
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _dangling(db: DB, name: str = "看病", *, times: int = 3) -> str:
    from gits.core.project import entity_id

    eid = entity_id(name)
    for i in range(times):
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


def _his(db: DB, ts: int, *, scope: str = "world") -> int:
    with Writer(db).txn() as c:
        cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                        " payload, lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                        (ts, ts, "shell", "msg_in", f"m{ts}{scope}", b"hi",
                         0, 1, scope))
        return int(cur.lastrowid)


# ── 1. 决定说 ≠ 说出去 ──────────────────────────────────────


def test_没投递就不算说过(db) -> None:
    """**关掉投递，做十次开口决定，未回复计数必须始终为零。**"""
    _dangling(db)
    _his(db, T0 - 2 * HOUR)
    for i in range(10):
        I.record(db, now=T0 - i * 60_000, reason="决定要说", candidate="看病")
    assert len(I.decided(db)) == 10, "决定要记下来"
    assert I.spoken(db) == [], "没发出去的不算说过"
    factor, weight = I.unanswered(db, now=T0 + HOUR)
    assert weight == 0.0, "为一句他从来没收到的话罚他"
    assert factor == 1.0
    assert I.pressure(db, now=T0) == pytest.approx(1.0), "压力不该被决定清零"


def test_投递了才算说过(db) -> None:
    _dangling(db)
    _his(db, T0 - 2 * HOUR)
    ev = I.record(db, now=T0 - 30 * 60_000, reason="说了", candidate="看病")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,?,'pending',?)", (T0, "weixin", "在吗", ev))
    assert I.spoken(db) == [T0 - 30 * 60_000]
    assert I.pressure(db, now=T0) < 1.0, "说出去了，压力该降"


# ── 2. 他回没回 ≠ 窗口里有没有消息 ──────────────────────────


def test_同伴的私信不算他的回应(db, monkeypatch) -> None:
    monkeypatch.setattr(I, "online", lambda db, ts, snap=None: 1.0)
    """B 和 C 的谈话永远不该让她相信"用户刚刚回应了我"。"""
    _dangling(db)
    _his(db, T0 - 2 * HOUR)
    ev = I.record(db, now=T0 - 60 * 60_000, reason="说了", candidate="看病")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,?,'sent',?)", (T0, "weixin", "在吗", ev))
    before = I.unanswered(db, now=T0)[0]
    # 两个同伴互发一条私信
    _his(db, T0 - 30 * 60_000, scope="self:peer2")
    _his(db, T0 - 20 * 60_000, scope="self:peer1")
    after = I.unanswered(db, now=T0)[0]
    assert after == pytest.approx(before), "同伴的私信被当成了他的回应"


def test_他回了才算回应(db, monkeypatch) -> None:
    monkeypatch.setattr(I, "online", lambda db, ts, snap=None: 1.0)
    _dangling(db)
    _his(db, T0 - 2 * HOUR)
    ev = I.record(db, now=T0 - 60 * 60_000, reason="说了", candidate="看病")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,?,'sent',?)", (T0, "weixin", "在吗", ev))
    before = I.unanswered(db, now=T0)[0]
    _his(db, T0 - 10 * 60_000)          # 他回了
    after = I.unanswered(db, now=T0)[0]
    assert after > before, "他真回了，欠账应该清零"


# ── 4. 上次确认在那 ≠ 现在还在那 ────────────────────────────


def _place_with_history(db: DB, *, last_fix: int) -> None:
    pid = 1
    with Writer(db).txn() as c:
        c.execute("INSERT INTO places(id, scope, lat, lon, n, first_ts, last_ts,"
                  " best_dwell_ms, name) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid, "self:her", 27.84, 120.83, 10, last_fix, last_fix,
                   3 * HOUR, "测试场所"))
        for i in range(3):
            c.execute("INSERT INTO place_fixes(place_id, event_id, ts, dwell_ms)"
                      " VALUES(?,?,?,?)", (pid, i + 1, last_fix - i * HOUR, HOUR))
        c.execute("INSERT INTO place_state(actor, place_id, since)"
                  " VALUES('her',?,?)", (pid, last_fix - 5 * HOUR))
    loc.record(db, lat=27.84, lon=120.83, acc=30, source="gps",
               fix_ts=last_fix, now=last_fix, city={"city": "温州市"})


def test_位置停更就不再说现在在那儿(db) -> None:
    """七天没上报，背景不能把"没有观测"写成"连续待了七天"。"""
    _place_with_history(db, last_fix=T0)
    fresh = B.build(db, now=T0 + HOUR)
    assert fresh.get("loc") == "测试场所"

    stale = B.build(db, now=T0 + 7 * DAY)
    assert "loc" not in stale, stale
    assert "stay" not in stale, stale


def test_坐标不留在幂等键里(db) -> None:
    """撤回清了 payload，坐标还留在 ext_id 里就等于没撤干净。"""
    loc.record(db, lat=27.123456, lon=120.654321, acc=30, source="gps",
               fix_ts=T0, now=T0)
    ext = db.scalar("SELECT ext_id FROM events WHERE source='phone'")
    assert "27.1" not in str(ext) and "120.6" not in str(ext), ext


def test_撤回之后不再参与学习(db) -> None:
    loc.record(db, lat=27.84, lon=120.83, acc=30, source="gps",
               fix_ts=T0, now=T0, city={"city": "温州市"})
    ev = db.scalar("SELECT id FROM events WHERE source='phone'")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO redactions(event_id, ts) VALUES(?,?)", (ev, T0))
    with Writer(db).txn() as c:
        for name, v in (("places", "DELETE FROM places"),
                        ("fixes", "DELETE FROM place_fixes"),
                        ("state", "DELETE FROM place_state"),
                        ("cursor", "DELETE FROM learn_cursor")):
            c.execute(v)
    assert places.learn(db) == 0, "撤回过的读数还在被学"


def test_同伴的私信不改用户活跃度(db) -> None:
    """活跃度只该由**他的**消息驱动。"""
    from gits.time import activity

    activity._cache.clear()
    # 他只在 10 点说话
    for d in range(7):
        _his(db, T0 - d * DAY + 10 * HOUR)
    # 同伴今天 14 点聊了五句
    for k in range(5):
        _his(db, T0 + 14 * HOUR + k * 60_000, scope="self:peer2")
    snap = activity.snapshot(db, now=T0)
    at_14 = snap.at(T0 + 14 * HOUR)
    assert at_14 == 0.0, f"同伴的对话把 14 点抬成了 {at_14:.3f}"


def test_共鸣在真实开口路径上生效(db) -> None:
    """以前只有 unresolved.inputs 算共鸣，initiative.assemble 传的是默认零——
    **设计里那部分影响不到任何真实判定。**"""
    from gits.memory.recall import reindex
    from gits.speech import initiative as I2
    from gits.speech.unresolved import scan as _scan

    eid = _dangling(db, "咖啡")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)",
                  ("n:coffee", "episode", T0 - HOUR, "world", 1.0, 1.0, T0 - HOUR))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", ("n:coffee", "今天又喝了很多咖啡", T0 - HOUR))
    reindex(db)
    cand = [c for c in _scan(db, now=T0) if c.entity == eid]
    assert cand, "候选里没有咖啡"
    i = I2.assemble(db, now=T0, actor="her", cand=cand[0])
    assert i.resonance > 0, "真实路径上的共鸣还是零"


# ── 3. 从旧版升级：不能把已经回过的重新变成待回复 ──────────


def test_升级后不回重答(tmp_path: Path) -> None:
    """旧版发过回复，但那时没有 replied 事件。**重开库时就该回填。**"""
    from gits.speech.respond import pending

    path = tmp_path / "old.db"
    db = DB(path)
    with Writer(db).txn() as c:
        cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                        " payload, lag, ts_trust, scope)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (T0, T0, "shell", "msg_in", "old-1", b"hi", 0, 1,
                         "world"))
        eid = int(cur.lastrowid)
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,?,'sent',?)", (T0 + 1000, "weixin", "嗯", eid))
    assert db.scalar("SELECT count(*) FROM events WHERE kind='replied'") == 0
    assert len(pending(db)) == 1, "升级前它是待回复的"
    db.close()

    db = DB(path)                   # ← 升级：重开就回填
    assert db.scalar("SELECT count(*) FROM events WHERE kind='replied'") == 1
    assert pending(db) == [], "升级之后又被当成没回过"
    db.close()
    db = DB(path)                   # 幂等
    assert db.scalar("SELECT count(*) FROM events WHERE kind='replied'") == 1
    db.close()


def test_回填用稳定身份跨库一致(db) -> None:
    """副本接管时行号不同，标记必须按 source+ext_id 对得上。"""
    with Writer(db).txn() as c:
        cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                        " payload, lag, ts_trust, scope)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (T0, T0, "shell", "msg_in", "abc", b"hi", 0, 1, "world"))
        eid = int(cur.lastrowid)
        c.execute("INSERT INTO outbox(ts, channel, text, status, ref_event_id)"
                  " VALUES(?,?,?,'sent',?)", (T0, "weixin", "嗯", eid))
    db._backfill_replied()
    ext = db.scalar("SELECT ext_id FROM events WHERE kind='replied'")
    assert ext == "replied:shell:abc", ext


# ── 5. 工具：失败和"没有"必须能分开 ────────────────────────


def test_工具失败不等于没有资料(db, monkeypatch) -> None:
    from gits.speech import tools_loop
    from gits.world import detail

    def boom(*a, **k):
        raise RuntimeError("数据库没了")

    called = []

    def chat(body, **kw):
        if kw.get("tools"):
            return type("R", (), {
                "text": "", "usage": {}, "latency_ms": 0,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "where", "arguments": "{}"}}],
            })()
        called.append(body[-1]["content"])
        return type("R", (), {"text": "好", "usage": {}, "latency_ms": 0,
                              "tool_calls": None})()

    tools_loop.run(chat, [{"role": "user", "content": "x"}],
                   catalog=({"name": "where", "about": "x"},),
                   detail=lambda kind, **kw: boom(), now=T0)
    assert called, "工具失败也要把话说完"
    assert '"error"' in called[0], f"失败被说成了空：{called[0]}"


def test_工具被禁用也是可区分的(db) -> None:
    """高频对话时工具不可用——这也不能表现成"他确实没资料"。"""
    from gits.world import detail

    got = detail(db, "where", now=T0)
    assert isinstance(got, dict)
