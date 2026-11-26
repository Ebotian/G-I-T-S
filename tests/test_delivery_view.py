"""影子校验：比的是"接下来该发什么"，不是节点数。

节点数一致、交付答案不同，才是会咬人的那种不一致。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.core.project import restore

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _msg(db: DB, ext: str, *, with_extract: bool = True) -> int:
    """造一条他发的消息。**带上抽取结果**，否则 restore 会抛 MissingModelResult。

    那也是对的：重放不允许重新调模型，缺结果就是硬错误。"""
    import json

    with Writer(db).txn() as c:
        cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                        " payload, lag, ts_trust, scope)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (T0, T0, "shell", "msg_in", ext, b"hi", 0, 1, "world"))
        eid = int(cur.lastrowid)
    if with_extract:
        Writer(db).append_model_call(
            kind="extract", model="stub", event_id=eid,
            response=json.dumps({"summary": "打招呼", "entities": [],
                                 "claims": []}, ensure_ascii=False))
    return eid


def _reply(db: DB, ext: str) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                  " ts_trust, scope) VALUES(?,?,?,?,?,?,?)",
                  (T0, T0, "ghost", "replied", f"replied:shell:{ext}", 1, "world"))


def test_交付视图按稳定身份(db) -> None:
    from gits.cli import _delivery_view

    a = _msg(db, "a")
    _msg(db, "b")
    _reply(db, "a")
    view = _delivery_view(db)
    assert view["pending"] == ["shell:b"], view
    assert view["replied"] == 1
    assert "shell:a" not in str(view), "已回复的那条还在待回复里"
    for item in view["pending"]:
        assert not item.split(":")[-1].isdigit(), f"待回复里是行号不是身份：{item}"


def test_行号不同但视图相同(db, tmp_path: Path) -> None:
    """副本用不同的行号重建同一段经历——交付视图必须一致。"""
    from gits.cli import _delivery_view

    _msg(db, "a")
    _msg(db, "b")
    _reply(db, "a")
    live = _delivery_view(db)

    other = DB(tmp_path / "copy.db")
    with Writer(other).txn() as c:                  # 先塞一条垃圾事件，把行号顶偏
        c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                  " payload, lag, ts_trust, scope)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (T0 - 99, T0 - 99, "self", "inner", "noise", b"x", 0, 1, "world"))
    for ext in ("a", "b"):      # 同样的顺序，行号却因为前面那条噪声整体偏移
        _msg(other, ext)
    _reply(other, "a")
    assert _delivery_view(other) == live
    other.close()


def test_重放之后视图不变(db, tmp_path: Path) -> None:
    """投影被清空重建，交付答案不该变——这正是影子校验要保证的。"""
    from gits.cli import _delivery_view

    _msg(db, "a")
    _msg(db, "b")
    _reply(db, "a")
    before = _delivery_view(db)
    restore(db)
    assert _delivery_view(db) == before
