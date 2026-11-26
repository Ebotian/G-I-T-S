"""在线率：细桶自己估，空白退回到小时。

三条，每条对应一种错法：
  空白不收缩     → 凌晨那段时间估出 0，然后压力永远攒不动
  密桶不独立     → 他每天 14:05 准时说话，却只能学到"14 点这个小时"
  快照不失效     → 新消息进来了还用旧估计，判定滞后
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.time import activity

# 2026-11-26 是周四
T0 = int(dt.datetime(2026, 11, 26, 0, 0).timestamp() * 1000)
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()
    activity._cache.clear()


def _msg(db: DB, ts: int) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                  " payload, lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                  (ts, ts, "shell", "msg_in", f"m{ts}", b"hi", 0, 1, "world"))


def _daily(db: DB, *, hour: int, minute: int = 0, days: int = 14) -> None:
    for d in range(days):
        _msg(db, T0 - d * DAY + hour * 3_600_000 + minute * 60_000)


def test_空白格退回到小时估计(db) -> None:
    """那个小时有数据、这个五分钟格没有 —— 退回小时，不发明。"""
    # 15 点里散着说（:00 / :20 / :40），但 :10 那个细格是空的
    for d in range(14):
        for m in (0, 20, 40):
            _msg(db, T0 - d * DAY + 15 * 3_600_000 + m * 60_000)
    snap = activity.snapshot(db, now=T0)
    empty_cell = T0 + 15 * 3_600_000 + 10 * 60_000
    assert snap.hour(empty_cell) > 0, "这个小时本来就有数据"
    assert snap.at(empty_cell) == pytest.approx(
        activity.PRIOR_DAYS * snap.hour(empty_cell)
        / (snap.days + activity.PRIOR_DAYS), abs=0.02)


def test_整小时都空就是零(db) -> None:
    """他凌晨从来不说话 —— 那就是 0，压力本来也不该在那时候攒。"""
    _daily(db, hour=15)
    snap = activity.snapshot(db, now=T0)
    assert snap.at(T0 + 3 * 3_600_000) == 0.0


def test_密桶有自己的分辨率(db) -> None:
    """他每天 14:05 说话——细桶应该比同一小时的其他分钟高。"""
    _daily(db, hour=14, minute=5)
    snap = activity.snapshot(db, now=T0)
    at_05 = snap.at(T0 + 14 * 3_600_000 + 5 * 60_000)
    at_55 = snap.at(T0 + 14 * 3_600_000 + 55 * 60_000)
    assert at_05 > at_55, (at_05, at_55)


def test_同一格里稳定(db) -> None:
    _daily(db, hour=15)
    snap = activity.snapshot(db, now=T0)
    a = snap.at(T0 + 15 * 3_600_000 + 60_000)
    b = snap.at(T0 + 15 * 3_600_000 + 4 * 60_000)
    assert a == b, "同一个五分钟格里，答案应该一样"


def test_快照随新消息失效(db) -> None:
    _daily(db, hour=15)
    s1 = activity.snapshot(db, now=T0)
    before = s1.at(T0 + 9 * 3_600_000)
    for i in range(5):
        _msg(db, T0 + 9 * 3_600_000 + i * 60_000)
    s2 = activity.snapshot(db, now=T0)
    assert s2.marker != s1.marker or s2 is not s1, "新消息进来了还在用旧快照"
    assert s2.at(T0 + 9 * 3_600_000) > before


def test_没有消息时是不知道不是五成(db) -> None:
    snap = activity.snapshot(db, now=T0)
    assert snap.hour(T0) == 0.5
