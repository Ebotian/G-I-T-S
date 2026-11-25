"""ghost：心跳、投递、快照。

三条要守的性质：
  心跳按桶去重（同一桶重复 tick 不能写两条）
  投递**真的执行**（文件通道能证明），过期的如实记 dropped
  没有通道时不许假装发出去
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from gits.db import DB, Writer, now_ms
from gits.ghost import FileTransport, Ghost, NullTransport, serve, sd_notify

T0 = 1_800_000_000_000
MIN = 60_000


def _db(tmp_path: Path) -> DB:
    return DB(tmp_path / "g.db")


def _pending(db: DB, text: str = "在吗", ts: int = T0) -> int:
    with Writer(db).txn() as c:
        cur = c.execute(
            "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,'pending')",
            (ts, "desk_audio", text))
        return int(cur.lastrowid)


# ── 心跳 ────────────────────────────────────────────────────
def test_同一桶只写一条心跳(tmp_path: Path) -> None:
    with _db(tmp_path) as db:
        g = Ghost(db=db)
        assert g.beat(T0) is True
        assert g.beat(T0 + 1000) is False        # 同一桶
        assert db.scalar("SELECT count(*) FROM events WHERE kind='vital'") == 1


def test_换桶就再写一条(tmp_path: Path) -> None:
    with _db(tmp_path) as db:
        g = Ghost(db=db)
        g.beat(T0)
        assert g.beat(T0 + g.heartbeat_ms) is True
        assert db.scalar("SELECT count(*) FROM events WHERE kind='vital'") == 2


def test_心跳的_ext_id_带节点名(tmp_path: Path) -> None:
    """多端之后每个节点各写各的，不能撞键。"""
    with _db(tmp_path) as db:
        Ghost(db=db, node="office").beat(T0)
        Ghost(db=db, node="cloud").beat(T0)
        rows = db.q("SELECT ext_id FROM events WHERE kind='vital' ORDER BY id")
        assert [r["ext_id"].split(":")[0] for r in rows] == ["office", "cloud"]


# 投递已经搬到 shell 与 delivery，测试见 tests/test_shell.py

# ── 节流 ────────────────────────────────────────────────────
def test_一拍里各项按自己的间隔触发(tmp_path: Path) -> None:
    with _db(tmp_path) as db:
        g = Ghost(db=db)
        first = g.tick(T0)
        assert first.get("beat") is True and first.get("snapshot") is True
        second = g.tick(T0 + 1000)               # 一秒后：什么都不该到期
        assert second == {}


def test_快照会写进_state_snapshot(tmp_path: Path) -> None:
    with _db(tmp_path) as db:
        Ghost(db=db).snapshot(T0)
        row = db.one("SELECT phase, backlog, note FROM state_snapshot WHERE ts=?", (T0,))
        assert row["phase"] == "AWAKE" and row["note"] == "cloud"


# ── 运行与退出 ──────────────────────────────────────────────
def test_跑一拍就退出(tmp_path: Path) -> None:
    p = tmp_path / "g.db"
    assert asyncio.run(serve(p, once=True)) == 0
    with DB(p) as db:
        assert db.scalar("SELECT count(*) FROM events WHERE kind='vital'") == 1


def test_没有_notify_socket_时安静返回() -> None:
    old = os.environ.pop("NOTIFY_SOCKET", None)
    try:
        assert sd_notify("READY=1") is False
    finally:
        if old is not None:
            os.environ["NOTIFY_SOCKET"] = old
