"""外部预测：**权威给的，不是我们推的。**

它不是读数，是记录——有效期在将来，所以进 C 段、且只在窗口内出现，
和生日那条通道（facts.due 的 lead_days）是同一个形状。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.world import forecast as F

NOW = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


def _cast(date: str, day: str, night: str, hi: float, lo: float) -> dict:
    return {"date": date, "dayweather": day, "nightweather": night,
            "daytemp": hi, "nighttemp": lo, "daywind": "北", "daypower": "3"}


def _fc(casts: list[dict]) -> dict:
    return {"kind": "forecast", "adcode": "330303", "city": "温州市",
            "reporttime": "2026-11-26 14:00:00", "casts": casts}


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_预报落成事件且幂等(db) -> None:
    fc = _fc([_cast("2026-11-27", "小雨", "小雨", 12.0, 6.0)])
    a, ca = F.record(db, fc, now=NOW)
    b, cb = F.record(db, fc, now=NOW)
    assert ca and not cb and a == b


def test_只看窗口内(db) -> None:
    """**今天也算**（下午还有一半），两天以后的不算——窗口是 lead_days。"""
    fc = _fc([
        _cast("2026-11-26", "小雨", "小雨", 12.0, 6.0),   # 今天
        _cast("2026-11-27", "小雪", "多云", 4.0, -3.0),   # 明天
        _cast("2026-11-28", "中雪", "中雪", 1.0, -8.0),   # 后天
        _cast("2026-11-29", "晴", "晴", 5.0, -4.0),
    ])
    F.record(db, fc, now=NOW)
    got = F.due(db, NOW)
    assert [c["date"] for c, _left in got] == ["2026-11-26", "2026-11-27"]
    assert [left for _c, left in got] == [0, 1]


def test_平淡的天气不打扰(db) -> None:
    """每天都说"明天多云"就变噪音。只报和平时不一样的。"""
    fc = _fc([_cast("2026-11-27", "多云", "多云", 20.0, 14.0)])
    F.record(db, fc, now=NOW)
    assert F.due(db, NOW) == []
    assert F.lines(db, NOW) == ()


def test_温差大也算值得说(db) -> None:
    fc = _fc([_cast("2026-11-27", "晴", "晴", 32.0, 12.0)])
    F.record(db, fc, now=NOW)
    assert len(F.due(db, NOW)) == 1


def test_只说天气不说建议(db) -> None:
    fc = _fc([_cast("2026-11-27", "小雨", "阴", 12.0, 6.0)])
    F.record(db, fc, now=NOW)
    line = F.lines(db, NOW)[0]
    assert "明天" in line and "雨" in line and "6" in line and "12" in line
    for word in ("带伞", "记得", "注意", "小心", "建议"):
        assert word not in line


def test_没有预报就是没有(db) -> None:
    assert F.due(db, NOW) == []
    assert F.latest(db) is None
