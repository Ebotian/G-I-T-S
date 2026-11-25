"""投递状态机 + shell 的收发闭环。

要守的四件事：
    租约防重复（两个 shell 同时跑不会发两次）
    重试有上限（无限重试 = 无限骚扰）
    过期如实记 dropped(expired)
    收进来的消息挂到最近一条已发送的 outbox 上（`outcomes` 不能是空的）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gits.db import DB, Writer, now_ms
from gits.delivery import claim, dispatch, expire
from gits.shell import FileChannel, Shell, serve

T0 = 1_800_000_000_000
MIN = 60_000


def _pending(db: DB, text: str = "在吗", ts: int = T0) -> int:
    with Writer(db).txn() as c:
        cur = c.execute(
            "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,'pending')",
            (ts, "desk_audio", text))
        return int(cur.lastrowid)


class Boom:
    name = "boom"

    def deliver(self, row):
        raise RuntimeError("通道炸了")

    def poll(self):
        return []


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


# ── 状态机 ──────────────────────────────────────────────────
def test_租约期内别人领不走(db) -> None:
    _pending(db)
    assert len(claim(db, now=T0, lease_ms=MIN)) == 1
    assert claim(db, now=T0 + 1000, lease_ms=MIN) == []


def test_租约过期可以重领且次数增加(db) -> None:
    _pending(db)
    claim(db, now=T0, lease_ms=MIN)
    rows = claim(db, now=T0 + 2 * MIN, lease_ms=MIN)
    assert len(rows) == 1 and rows[0]["attempts"] == 2


def test_超过重试上限就记_failed(db) -> None:
    out = tmp_path_out = None
    with DB(db.path) as d:
        oid = _pending(d)
        for i in range(4):
            dispatch(d, Boom(), now=T0 + i * MIN, max_attempts=3, ttl_ms=10 * MIN)
        assert d.scalar("SELECT status FROM outbox WHERE id=?", (oid,)) == "failed"


def test_过期记_dropped(db) -> None:
    oid = _pending(db)
    assert expire(db, now=T0 + 60 * MIN, ttl_ms=10 * MIN) == 1
    assert db.scalar("SELECT status FROM outbox WHERE id=?", (oid,)) == "dropped"


def test_成功后写一条_delivered(db, tmp_path: Path) -> None:
    oid = _pending(db)
    ch = FileChannel(tmp_path / "o.jsonl", tmp_path / "i.jsonl")
    r = dispatch(db, ch, now=T0 + MIN)
    assert r.sent == 1
    assert db.scalar("SELECT count(*) FROM outcomes WHERE outbox_id=? AND kind='delivered'",
                     (oid,)) == 1


# ── 收发闭环 ────────────────────────────────────────────────
def test_发出去的内容真的落了盘(db, tmp_path: Path) -> None:
    _pending(db, "今天好安静")
    ch = FileChannel(tmp_path / "o.jsonl", tmp_path / "i.jsonl")
    dispatch(db, ch, now=T0 + MIN)
    assert "今天好安静" in (tmp_path / "o.jsonl").read_text(encoding="utf-8")


def test_收进来的变成事件(db, tmp_path: Path) -> None:
    inbox = tmp_path / "i.jsonl"
    inbox.write_text('{"text": "在的", "ext_id": "m1", "ts": %d}\n' % T0,
                     encoding="utf-8")
    shell = Shell(db=db, channels=[FileChannel(tmp_path / "o.jsonl", inbox)])
    assert shell.intake(T0 + MIN) == 1
    assert db.scalar("SELECT count(*) FROM events WHERE kind='msg_in'") == 1
    # 再收一次不能重复
    assert shell.intake(T0 + 2 * MIN) == 0


def test_回复挂到最近一条已发送上(db, tmp_path: Path) -> None:
    oid = _pending(db)
    ch = FileChannel(tmp_path / "o.jsonl", tmp_path / "i.jsonl")
    dispatch(db, ch, now=T0 + MIN)
    (tmp_path / "i.jsonl").write_text('{"text":"在的","ext_id":"r1"}\n',
                                      encoding="utf-8")
    shell = Shell(db=db, channels=[ch])
    shell.intake(T0 + 2 * MIN)
    row = db.one("SELECT outbox_id, kind FROM outcomes WHERE kind='reply'")
    assert row is not None and row["outbox_id"] == oid


def test_一拍同时收发(db, tmp_path: Path) -> None:
    _pending(db)
    (tmp_path / "i.jsonl").write_text('{"text":"在","ext_id":"x"}\n', encoding="utf-8")
    shell = Shell(db=db, channels=[FileChannel(tmp_path / "o.jsonl",
                                               tmp_path / "i.jsonl")])
    did = shell.tick(T0 + MIN)
    assert "flush" in did and "intake" in did


def test_跑一拍就退出(db, tmp_path: Path) -> None:
    p = Path(db.path)
    rc = asyncio.run(serve(p, out_file=tmp_path / "o.jsonl",
                           in_file=tmp_path / "i.jsonl", once=True))
    assert rc == 0
