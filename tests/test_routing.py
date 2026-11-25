"""通道名路：谁认领哪一行的 outbox。

没有这一层，文件通道会把微信的活儿也领走——然后"发到文件里"，
而你以为发出去了。**发错地方和没发一样糟，但更难发现。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.db import DB, Writer
from gits.delivery import claim, dispatch
from gits.shell import FileChannel

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


class FakeWeixin:
    name = "weixin"
    names = frozenset({"weixin"})

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def deliver(self, row):
        self.sent.append((row["channel"], row["text"]))
        return "sent", "m1"

    def poll(self):
        return []


def _row(db: DB, channel: str, text: str = "在吗") -> int:
    with Writer(db).txn() as c:
        return int(c.execute(
            "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,'pending')",
            (T0, channel, text)).lastrowid)


def test_文件通道不领微信的活(db, tmp_path: Path) -> None:
    _row(db, "weixin", "这句该走微信")
    ch = FileChannel(tmp_path / "o", tmp_path / "i")
    assert dispatch(db, ch, now=T0) .sent == 0
    assert db.scalar("SELECT status FROM outbox") == "pending"


def test_微信通道只领自己的(db, tmp_path: Path) -> None:
    _row(db, "weixin", "a")
    _row(db, "desk_audio", "b")
    fake = FakeWeixin()
    dispatch(db, fake, now=T0)
    assert [t for _c, t in fake.sent] == ["a"]


def test_文件通道领_desk_audio(db, tmp_path: Path) -> None:
    _row(db, "desk_audio", "播出来")
    ch = FileChannel(tmp_path / "o", tmp_path / "i")
    assert dispatch(db, ch, now=T0).sent == 1


def test_claim_按名字过滤(db) -> None:
    _row(db, "weixin")
    _row(db, "file")
    assert len(claim(db, now=T0, names=frozenset({"weixin"}))) == 1
    assert len(claim(db, now=T0, names=None)) == 2
