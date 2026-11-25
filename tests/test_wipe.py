"""清空内容、保留机制——这条也得有测试。

它的失败方式是"看起来清干净了，其实重放又回来了"：
payload 清了但投影没清，或者清了 payload 没清 model_calls。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits import persona, project, wipe
from gits.db import DB, Writer

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_清完之后A段是空的(db) -> None:
    persona.seed(db, "我不太会哄人。", now=T0)
    assert persona.current(db) is not None
    got = wipe.wipe(db, now=T0)
    assert got == {"seed": 1}
    assert persona.current(db) is None
    assert wipe.states(db) == {}


def test_机制还在(db) -> None:
    persona.seed(db, "我不太会哄人。", now=T0)
    wipe.wipe(db, now=T0)
    # 表还在，能接着写；只是没有内容
    persona.seed(db, "重新开始。", now=T0 + 1)
    assert persona.current(db)["narrative"] == "重新开始。"


def test_重放不会把内容造回来(db) -> None:
    persona.seed(db, "我不太会哄人。", now=T0)
    project.restore(db)
    assert db.scalar("SELECT count(*) FROM nodes WHERE scope='self:her'") == 1
    wipe.wipe(db, now=T0)
    project.restore(db)
    assert db.scalar("SELECT count(*) FROM nodes WHERE scope='self:her'") == 0, \
        "payload 清了，重放就不该再把它造出来"


def test_删除本身是事实(db) -> None:
    ev = persona.seed(db, "我不太会哄人。", now=T0)
    wipe.wipe(db, now=T0)
    assert db.scalar("SELECT count(*) FROM redactions WHERE event_id=?", (ev,)) == 1
    assert db.scalar("SELECT count(*) FROM events WHERE kind='redact'") == 1, \
        "「曾经有过，后来清掉了」也是记录"


def test_清空不动别人的内容(db) -> None:
    from gits.scope import self_of

    persona.seed(db, "她的", now=T0)
    with Writer(db).txn() as c:
        c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id, payload,"
                  " lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                  (T0, T0, "seed", "seed", "peer1-seed", b"\xe5\x88\xab\xe4\xba\xba",
                   0, 1, self_of("peer1")))
    wipe.wipe(db, actor="her", now=T0)
    row = db.one("SELECT payload FROM events WHERE ext_id='peer1-seed'")
    assert row["payload"] is not None, "只清她自己的"


def test_清空也清注意力权重(db) -> None:
    from gits import attention

    attention.seed(db, "her", {"开会": 1.2})
    assert attention.weights(db, "her") == {"开会": 1.2}
    got = wipe.wipe(db, now=T0)
    assert got.get("attention_weights") == 1
    assert attention.weights(db, "her") == {}, \
        "权重是内容，不清的话「清干净了」就是假的"


def test_清空的返回值对得上状态(db) -> None:
    persona.seed(db, "一段", now=T0)
    from gits import attention

    attention.seed(db, "her", {"a": 1.0})
    got = wipe.wipe(db, now=T0)
    assert wipe.states(db) == {}
    assert got["seed"] == 1 and got["attention_weights"] == 1
