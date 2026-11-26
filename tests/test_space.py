"""空间源：我们接收，不追踪。

三条约束，每条都对应实测过的坏法：

  1 方向只能是手机 -> 服务器   手机在 CGNAT 后面，实测 IPv4 是 10.159.144.128
  2 坐标换标签才出门           原值进上下文 = 每天上百次把精确位置发给模型
  3 带证据等级                 ip 的误差会让"家"和"公司"合并成一个点
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.core.db import DB
from gits.world import loc

T0 = 1_800_000_000_000
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_位置落成读数事件(db) -> None:
    _ev, created = loc.record(db, lat=27.83941, lon=120.837397, acc=30,
                              source="gps", fix_ts=T0, now=T0)
    assert created
    assert db.scalar("SELECT count(*) FROM events WHERE kind='reading'") == 1


def test_重复上报不产生重复轨迹(db) -> None:
    """手机重发同一次定位，不该在轨迹上多一个点。"""
    kw = dict(lat=27.83941, lon=120.837397, acc=30, source="gps", fix_ts=T0)
    a, ca = loc.record(db, now=T0, **kw)
    b, cb = loc.record(db, now=T0 + 60_000, **kw)
    assert ca is True
    assert b == a, "重发不该产生新事件"
    assert cb is False
    assert db.scalar("SELECT count(*) FROM events WHERE kind='reading'") == 1


def test_位置是私有的(db) -> None:
    """他的位置只给 her 看。peer 不该知道他在哪。"""
    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0, now=T0)
    assert loc.latest(db, now=T0, scope="self:her") is not None
    assert loc.latest(db, now=T0, scope="self:peer1") is None
    assert loc.latest(db, now=T0, scope="world") is None


def test_重放之后位置还在(db) -> None:
    from gits.core import project

    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0, now=T0)
    project.restore(db)
    assert loc.latest(db, now=T0) is not None
    assert db.scalar("SELECT count(*) FROM nodes") == 0, "位置不是记忆"


def test_只说出城市不说坐标(db) -> None:
    """原值出门等于每天上百次把精确位置发给模型。"""
    loc.record(db, lat=27.83941, lon=120.837397, acc=15, source="gps",
               fix_ts=T0, now=T0, city={"province": "浙江省", "city": "温州市",
                                        "district": "龙湾区", "adcode": "330303"})
    got = loc.line(db, now=T0)
    assert "温州市" in got
    for leak in ("27.8", "120.8", "龙湾", "330303"):
        assert leak not in got, f"标签里漏了原值：{got}"


def test_过期就不说(db) -> None:
    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0, now=T0,
               city={"city": "温州市"})
    assert loc.line(db, now=T0 + HOUR)
    assert loc.line(db, now=T0 + 5 * HOUR) == ""


def test_没有读数就是没有不是消失(db) -> None:
    """沉默不能当负面。没有读数 = 我不知道，不是"他不见了"。"""
    assert loc.line(db, now=T0) == ""
    assert loc.fresh(db, now=T0) is None


def test_天气跟着他走(db, monkeypatch) -> None:
    """有位置就用他的城市；配置只是兜底。"""
    from gits.world import amap

    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0, now=T0,
               city={"city": "温州市", "adcode": "330303"})
    seen: dict = {}

    def fake(adcode, *, key):
        seen["adcode"] = adcode
        return {"kind": "weather", "adcode": adcode, "city": "温州市",
                "weather": "晴", "temp": 20.0, "humidity": 50.0,
                "wind": "东", "power": "2", "report": "2026-11-26 15:00:00"}

    monkeypatch.setattr(amap, "fetch_weather", fake)
    monkeypatch.setenv("GITS_AMAP_KEY", "k")
    monkeypatch.setenv("GITS_AMAP_ADCODE", "330100")      # 机房：杭州
    key, configured = amap.config()
    amap.maybe(db, now=T0, key=key, adcode=loc.adcode(db, now=T0) or configured)
    assert seen["adcode"] == "330303", "天气没用他的城市"


def test_场所层只收够精度的来源(db) -> None:
    """ip 定位几十公里误差，会把家和公司合并成一个点。"""
    assert "ip" not in loc.PLACE_SOURCES
    assert {"gps", "wifi"} <= loc.PLACE_SOURCES


# ── 时间戳必须由手机自己盖 ──────────────────────────────────


def test_手机盖的时间戳优先于收件时间(db) -> None:
    """termux-location 0.60 不返回定位时间，所以手机端自己塞一个 phone_ts。

    否则时间戳只是收件时间——手机离线三小时补报也会显得"刚刚"。
    """
    phone_ts = T0 - 3 * HOUR
    _ev, created = loc.record(db, lat=27.8, lon=120.8, source="gps",
                              fix_ts=phone_ts, now=T0)
    assert created
    row = db.one("SELECT ts, recv_ts, lag FROM events WHERE source='phone'")
    assert int(row["ts"]) == phone_ts, "定位时间被收件时间顶替了"
    assert int(row["recv_ts"]) == T0
    assert int(row["lag"]) == 3 * HOUR, "传输延迟没记下来"


def test_补报的旧定位不会显得刚刚(db) -> None:
    """按 fix_ts 判新鲜度，不按收件时间。"""
    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0 - 5 * HOUR,
               now=T0, city={"city": "温州市"})
    assert loc.fresh(db, now=T0) is None, "三小时前的定位被当成此刻"
    assert loc.line(db, now=T0) == ""


def test_重发同一条定位不产生新事件(db) -> None:
    kw = dict(lat=27.8, lon=120.8, source="gps", fix_ts=T0)
    a, _ = loc.record(db, now=T0, **kw)
    b, created = loc.record(db, now=T0 + 600_000, **kw)
    assert (b, created) == (a, False), "手机重发同一条定位，账上多了一条"


def test_五毫秒返回的是缓存(db) -> None:
    """elapsedMs 很小 = termux-location 返回的是最后已知位置。"""
    _ev, _ = loc.record(db, lat=27.8, lon=120.8, source="network",
                        fix_ts=T0, now=T0, elapsed_ms=5)
    import json

    p = json.loads(db.scalar("SELECT payload FROM events WHERE source='phone'").decode())
    assert p["cached"] is True
    assert p["elapsed_ms"] == 5


def test_真的取一次就不是缓存(db) -> None:
    import json

    loc.record(db, lat=27.8, lon=120.8, source="gps", fix_ts=T0, now=T0,
               elapsed_ms=1800)
    p = json.loads(db.scalar("SELECT payload FROM events WHERE source='phone'").decode())
    assert p["cached"] is False
