"""审计提的四条，各有一组测试。它们失败的方式不一样：

  游标     先存游标后落库 → 崩一次就丢一条消息（服务端不会重发）
  路由     回复发给"最近联系人" → 两条消息交错时发给错的人
  历史     只写 outbox 不写 msg_out → 她忘记自己刚说过什么
  副本     先拉 model_calls → 引用对不上（而且 copy 的是对面的本地 id）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import replica
from gits.db import DB, Writer
from gits.delivery import dispatch
from gits.respond import recent
from gits.shell import Shell

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


class FakeChannel:
    """能假装"这一批收到了、但还没落库"。"""

    name = "weixin"
    names = frozenset({"weixin"})

    def __init__(self, items):
        self.items = items
        self.acked = 0
        self.cursor_saved = False

    def poll(self):
        return self.items

    def ack(self):
        self.acked += 1
        self.cursor_saved = True

    def deliver(self, row):
        return "sent", "m1"


# ── 游标 ─────────────────────────────────────────────────────


def test_游标只在落库之后才确认(db) -> None:
    ch = FakeChannel([{"text": "在吗", "ext_id": "m1", "from": "u1"}])
    sh = Shell(db=db, channels=[ch])
    assert sh.intake(t=T0) == 1
    assert ch.acked == 1, "消息落库了，通道才该推进游标"
    assert db.scalar("SELECT count(*) FROM events WHERE kind='msg_in'") == 1


def test_通道说没收到就什么都不写(db) -> None:
    ch = FakeChannel([])
    sh = Shell(db=db, channels=[ch])
    assert sh.intake(t=T0) == 0
    assert db.scalar("SELECT count(*) FROM events") == 0


# ── 路由 ─────────────────────────────────────────────────────


def _msg(db, text, ext, actor, token) -> int:
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id=ext,
                                    ts=T0, recv_ts=T0, payload=text.encode())
    with Writer(db).txn() as c:
        c.execute("INSERT INTO event_route(event_id, channel, actor_id,"
                  " context_token) VALUES(?,?,?,?)", (ev, "weixin", actor, token))
    return ev


def test_回复发给触发它的那个人(db) -> None:
    from gits.respond import respond

    class Stub:
        pass

    from gits.persona import seed
    seed(db, "我不太会哄人。", now=T0)
    _msg(db, "我是 A", "m1", "uA", "tokA")
    ev_b = _msg(db, "我是 B", "m2", "uB", "tokB")

    import gits.respond as R

    orig = R.chat
    try:
        R.chat = lambda *a, **k: __import__("gits.llm", fromlist=["Reply"]).Reply(
            text="好", model="stub", usage={"prompt_tokens": 100}, latency_ms=1)
        turn = respond(db, dict(db.one("SELECT * FROM events WHERE id=?", (ev_b,))))
    finally:
        R.chat = orig

    row = db.one("SELECT channel, meta FROM outbox WHERE id=?", (turn.outbox_id,))
    assert row["channel"] == "weixin"
    meta = json.loads(row["meta"])
    assert meta["to"] == "uB" and meta["context_token"] == "tokB", \
        "必须是 B 的 token，不是最近联系人的"


def test_第二个人进不来(db) -> None:
    sh = Shell(db=db, channels=[FakeChannel([
        {"text": "我是第一个", "ext_id": "m1", "from": "uA"}])])
    sh.intake(t=T0)
    sh2 = Shell(db=db, channels=[FakeChannel([
        {"text": "我是第二个", "ext_id": "m2", "from": "uB"}])])
    assert sh2.intake(t=T0) == 0
    assert sh2.dropped == 1
    assert db.scalar("SELECT count(*) FROM events WHERE kind='msg_in'") == 1


# ── 历史 ─────────────────────────────────────────────────────


def test_发出去的回复会变成她的历史(db) -> None:
    with Writer(db).txn() as c:
        oid = int(c.execute(
            "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,'pending')",
            (T0, "weixin", "我在。")).lastrowid)
    dispatch(db, FakeChannel([]), now=T0)
    assert db.scalar("SELECT count(*) FROM events WHERE kind='msg_out'") == 1
    assert db.scalar("SELECT count(*) FROM events WHERE ext_id=?",
                     (f"outbox:{oid}",)) == 1


def test_下一轮能读到她自己说的话(db) -> None:
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="在吗".encode())
    Writer(db).append_event(source="weixin", kind="msg_out", ext_id="outbox:1",
                            ts=T0 + 1, recv_ts=T0 + 1, payload="在。".encode())
    turns = recent(db)
    assert [t for _w, t in turns] == ["在吗", "在。"]
    assert [w for w, _t in turns] == ["他", "我"]


def test_窗口只装自己那一扇(db) -> None:
    for i in range(3):
        Writer(db).append_event(source="shell", kind="msg_in", ext_id=f"old{i}",
                                ts=T0 + i, recv_ts=T0 + i, payload=b"old")
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="new",
                                    ts=T0 + 10, recv_ts=T0 + 10, payload=b"new")
    assert [t for _w, t in recent(db, from_id=ev)] == ["new"]


# ── 副本 ─────────────────────────────────────────────────────


def test_副本先拉事件再拉依赖(db, tmp_path: Path) -> None:
    src = DB(tmp_path / "src.db")
    try:
        ev, _ = Writer(src).append_event(source="shell", kind="msg_in",
                                         ext_id="m1", ts=T0, recv_ts=T0,
                                         payload=b"hi")
        Writer(src).append_model_call(kind="extract", model="x", response="{}",
                                      event_id=ev)
        replica.pull(src, db, "rep", limit_rows=1)   # 一批只拉一行
        for _ in range(3):
            replica.pull(src, db, "rep", limit_rows=1)
    finally:
        src.close()
    got = db.scalar("SELECT event_id FROM model_calls ORDER BY id LIMIT 1")
    assert got == db.scalar("SELECT id FROM events WHERE ext_id='m1'"), \
        "引用必须指向本地那条事件"
