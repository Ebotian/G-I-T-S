"""外面的世界：读数必须是事件。

三条不变式，每条都对应一个具体会坏的地方：

  ① 读数是事件      装配时现取 → 重放拿到不同天气 → "她那天说的话"不可复现
  ② 位置是配置      她跑在 ECS 上，IP 定位给的是**机房**所在城市
  ③ 读数不触发开口  "下雨了" 进 B 段，不进 gate 的 reason
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech import context as C
from gits.world import amap

T0 = 1_800_000_000_000
HOUR = 3_600_000

#: 一份固定观测。**不联网**——测试不依赖天气好不好。
WEATHER = {
    "kind": "weather", "adcode": "330100", "city": "杭州市",
    "weather": "小雨", "temp": 11.0, "humidity": 88.0,
    "wind": "东北", "power": "4",
    "report": "2026-11-26 14:00:00",
}


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _report_ms(s: str) -> int:
    import datetime as dt

    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


# ── ① 读数是事件 ────────────────────────────────────────────


def test_读数落成事件且幂等(db) -> None:
    ev, created = amap.record(db, WEATHER, now=T0)
    assert created
    ev2, created2 = amap.record(db, WEATHER, now=T0)
    assert (ev2, created2) == (ev, False), "同一份观测不该落两次"
    assert db.scalar("SELECT count(*) FROM events WHERE kind='reading'") == 1


def test_重放之后读数还在(db) -> None:
    """装配只读日志——这是重放等价的前提。"""
    from gits.core import project

    amap.record(db, WEATHER, now=T0)
    project.restore(db)
    got = amap.latest(db, now=T0)
    assert got is not None and got["weather"] == "小雨"


def test_读数是事件但不是记忆(db) -> None:
    """reading 不投影成节点：她记得住的是经历，不是气温。"""
    from gits.core import project

    amap.record(db, WEATHER, now=T0)
    project.restore(db)
    assert db.scalar("SELECT count(*) FROM nodes") == 0


# ── ② 位置是配置 ────────────────────────────────────────────


def test_没配就不猜(monkeypatch) -> None:
    monkeypatch.delenv("GITS_AMAP_KEY", raising=False)
    monkeypatch.delenv("GITS_AMAP_ADCODE", raising=False)
    with pytest.raises(RuntimeError, match="GITS_AMAP_KEY"):
        amap.config()
    monkeypatch.setenv("GITS_AMAP_KEY", "k")
    with pytest.raises(RuntimeError, match="GITS_AMAP_ADCODE"):
        amap.config()


def test_IP定位只用来核对不覆盖(monkeypatch) -> None:
    """她跑在 ECS 上，IP 定位给的是机房城市。实测两台机器返回同一个城市。"""
    monkeypatch.setenv("GITS_AMAP_KEY", "k")
    monkeypatch.setenv("GITS_AMAP_ADCODE", "320100")     # 他给的：南京
    monkeypatch.setattr(amap, "fetch_ip", lambda **_: {"adcode": "330100"})
    key, adcode = amap.config()
    assert adcode == "320100", "IP 定位不许改配置"


# ── ③ 不触发开口，只说外面 ──────────────────────────────────


def test_过期读数不进上下文(db) -> None:
    rep = _report_ms(WEATHER["report"])
    amap.record(db, WEATHER, now=rep)
    assert amap.line(db, now=rep + HOUR), "一小时内该说"
    assert amap.line(db, now=rep + 5 * HOUR) == "", "过期了就不该说"


def test_说外面不说建议(db) -> None:
    rep = _report_ms(WEATHER["report"])
    amap.record(db, WEATHER, now=rep)
    got = amap.line(db, now=rep + HOUR)
    assert "小雨" in got and "11" in got
    for word in ("带伞", "记得", "注意", "小心"):
        assert word not in got, f"读数说了建议：{got}"


def test_没有读数就是空串(db) -> None:
    assert amap.line(db, now=T0) == ""


def test_大风才提风(db) -> None:
    rep = _report_ms(WEATHER["report"])
    amap.record(db, {**WEATHER, "power": "1-3"}, now=rep)
    assert "风" not in amap.line(db, now=rep), "三级以下的风不值得说"


def test_B段带上外面这一行(db) -> None:
    """接进 B 段，不是另起一段——她看到的是同一份此刻。"""
    rep = _report_ms(WEATHER["report"])
    amap.record(db, WEATHER, now=rep)
    snap = C.RelationSnapshot(bucket=0, summary="s", hubs=(), phase="醒着",
                              world=amap.line(db, now=rep))
    assert "小雨" in C.render_b(snap)
    assert C.render_b(C.RelationSnapshot(bucket=0, summary="s", hubs=(), phase="醒着"))
    assert "外面" not in C.render_b(C.RelationSnapshot(
        bucket=0, summary="s", hubs=(), phase="醒着"))


def test_取不到不抛(db, monkeypatch) -> None:
    """没有天气是正常状态，不能拖住 tick。"""
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(amap, "fetch_weather", boom)
    assert amap.maybe(db, now=T0, key="k", adcode="330100") is None


def test_同一份观测不重复采集(db, monkeypatch) -> None:
    monkeypatch.setattr(amap, "fetch_weather", lambda *a, **k: WEATHER)
    rep = _report_ms(WEATHER["report"])
    assert amap.maybe(db, now=rep, key="k", adcode="330100") is not None
    assert amap.maybe(db, now=rep + 2 * HOUR, key="k", adcode="330100") is None, \
        "观测没更新就不该再落一条"
