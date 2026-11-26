"""关系账本：**数出来的，不是记下来的。**

body_count 这类字段不能是字段。它一旦被"总结"就和事实永久分开，
而且**不能减**——减错了没有任何东西能改回来。

所以真相是事件，数字是投影。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.memory import relations as R

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_空账是空串(db) -> None:
    """没有发生的事不该占 token，也不该被模型补出来。"""
    assert R.brief(db, now=T0) == ""


def test_记一次就多一次(db) -> None:
    R.record(db, action="kiss", now=T0, src="world")
    R.record(db, action="kiss", now=T0 + DAY, src="world")
    assert R.counters(db, now=T0 + DAY)["kiss"] == 2


def test_body_count数的是人不是次数(db) -> None:
    """同一个人十次，body_count 还是 1——这是它和 intimacy 的区别。"""
    for i in range(10):
        R.record(db, action="intimacy", now=T0 + i * DAY, src="world")
    c = R.counters(db, now=T0 + 9 * DAY)
    assert c["intimacy"] == 10
    assert c["body_count"] == 1


def test_body_count只增不减(db) -> None:
    """**没有"改小"这个操作。** 记账是追加，不是赋值。"""
    R.record(db, action="intimacy", now=T0, src="world")
    before = R.counters(db, now=T0)["body_count"]
    R.record(db, action="breakup", now=T0 + DAY, src="world")
    after = R.counters(db, now=T0 + DAY)["body_count"]
    assert after >= before, "分手把 body_count 改小了"


def test_她不能自己记账(db) -> None:
    """模型自己想的'那晚'不算——这是"不能拿自己的输出当世界的证据"第六处。"""
    assert R.record(db, action="intimacy", now=T0, src="dream") is None
    assert "body_count" not in R.counters(db, now=T0)
    faults = db.q("SELECT payload FROM events WHERE kind='fault'")
    assert any(b"relation" in bytes(f["payload"]) for f in faults)


def test_动作必须在枚举里(db) -> None:
    """让模型自由发挥，账本里会多出"半次""差点"。"""
    assert R.record(db, action="half", now=T0, src="world") is None
    assert R.record(db, action="差点", now=T0, src="world") is None
    assert R.counters(db, now=T0) == {}


def test_一条事件只代表一次(db) -> None:
    """模型不能一次加 5——增量恒为 1，写死在代码里。"""
    R.record(db, action="kiss", now=T0, src="world")
    assert R.counters(db, now=T0)["kiss"] == 1
    # 同名同毫秒的重复写入靠稳定身份挡住
    R.record(db, action="kiss", now=T0, src="world")
    assert R.counters(db, now=T0)["kiss"] == 1


def test_重放之后账本一样(db) -> None:
    """账本是事件的函数——重放能重建，wipe 能清掉。"""
    R.record(db, action="intimacy", now=T0, src="world")
    R.record(db, action="kiss", now=T0 + DAY, src="world")
    before = R.counters(db, now=T0 + DAY)
    assert len(R.events(db)) == 2
    after = R.counters(db, now=T0 + DAY)
    assert before == after


def test_前史单独存_和事件相加(db) -> None:
    """认识他之前的部分推不出来，所以是记录；之后的部分是事件。"""
    R.seed(db, now=T0, body_count=2)
    R.record(db, action="intimacy", now=T0 + DAY, src="world")
    assert R.counters(db, now=T0 + DAY)["body_count"] == 3


def test_时间给天数不给时间戳(db) -> None:
    """上下文里出现毫秒时间戳没有意义，天数才有。"""
    R.record(db, action="kiss", now=T0, src="world")
    c = R.counters(db, now=T0 + 30 * DAY)
    assert c["since_first_d"] == 30
    assert "since_first" not in c, "把原始时间戳塞进上下文了"


def test_名字不像名字就拒(db) -> None:
    long_name = "x" * 100
    ev = R.record(db, action="kiss", now=T0, partner=long_name, src="world")
    assert ev is not None
    assert max(len(e["partner"]) for e in R.events(db)) <= R.MAX_PARTNER


def test_上下文里是数字不是句子(db) -> None:
    R.record(db, action="intimacy", now=T0, src="world")
    b = R.brief(db, now=T0)
    assert b.startswith("{") and "body_count" in b
    assert " " not in b, "brief 里出现了空格，那是句子不是数据"


def test_账本忽略不是枚举的动作(db) -> None:
    """万一有外来事件（同步进来的旧版本、手工改库），账本不认。"""
    from gits.core.db import Writer
    import json as _json

    Writer(db).append_event(
        source="self", kind="relation", ext_id="relation:half:0", ts=T0,
        recv_ts=T0, payload=_json.dumps({"action": "half", "partner": "他",
                                         "src": "world"}).encode())
    assert R.counters(db, now=T0) == {}
