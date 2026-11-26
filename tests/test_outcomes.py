"""回复要挂到**还没被回过**的那条 outbox 上。

第一版取"最近一条 sent"，于是同一行被挂了两次 reply——而她其实只被回了一次。
`influence` 的强化全靠这个关联（§3.2），重复一次就是把权重算重。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.io.shell import Shell

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _sent(db: DB) -> int:
    with Writer(db).txn() as c:
        return int(c.execute(
            "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,'sent')",
            (T0, "weixin", "在吗")).lastrowid)


def test_一条回复只挂一次(db) -> None:
    a = _sent(db)
    shell = Shell(db=db)
    shell._link_reply(T0 + 1)
    shell._link_reply(T0 + 2)          # 第二次没有可挂的行
    rows = db.q("SELECT outbox_id FROM outcomes WHERE kind='reply'")
    assert [r["outbox_id"] for r in rows] == [a]


def test_两条待回消息各挂各的(db) -> None:
    a, b = _sent(db), _sent(db)
    shell = Shell(db=db)
    shell._link_reply(T0 + 1)
    shell._link_reply(T0 + 2)
    ids = [r["outbox_id"] for r in db.q(
        "SELECT outbox_id FROM outcomes WHERE kind='reply' ORDER BY id")]
    assert ids == [b, a], "先挂最新的那条，再挂剩下的"


def test_没发过就不挂(db) -> None:
    shell = Shell(db=db)
    shell._link_reply(T0)
    assert db.scalar("SELECT count(*) FROM outcomes") == 0
