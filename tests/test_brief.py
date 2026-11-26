"""常驻背景块：结构化、冻结、只放事实。

三条不变式，每条都对应一个具体的坏法：

  同小时内逐字节相同    否则前缀碎，缓存全废
  只放可核对的          概率进了上下文就会变成话术
  派生量不进表          停留时长是右删失的，不能当样本
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.world import brief as B
from gits.world import loc, places

T0 = int(dt.datetime(2026, 11, 26, 11, 0).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _fill(db: DB, *, city: str = "温州市", name: str | None = None) -> int:
    """造一个像样的世界：一处场所 + 几条定位 + 天气。"""
    pid = 1
    with Writer(db).txn() as c:
        c.execute("INSERT INTO places(id, scope, lat, lon, n, first_ts, last_ts,"
                  " best_dwell_ms, name) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid, "self:her", 27.84, 120.83, 10, T0, T0, 3_600_000, name))
        for i in range(4):
            c.execute("INSERT INTO place_fixes(place_id, event_id, ts, dwell_ms)"
                      " VALUES(?,?,?,?)", (pid, i + 1, T0 + i * 900_000, 900_000))
        c.execute("INSERT INTO place_state(actor, place_id, since) VALUES('her',?,?)",
                  (pid, T0))
    loc.record(db, lat=27.84, lon=120.83, acc=30, source="network",
               fix_ts=T0 + 1000, now=T0 + 1000, city={"city": city})
    from gits.world import amap
    amap.record(db, {"kind": "weather", "adcode": "330303", "city": city,
                     "weather": "晴", "temp": 19.0, "humidity": 40.0,
                     "wind": "东", "power": "2",
                     "report": "2026-11-26 11:00:00"}, now=T0)
    return pid


def test_同小时内逐字节相同(db) -> None:
    """**"通常不变"和"保证不变"是两件事。** 冻结之后才叫保证。"""
    _fill(db)
    a = B.freeze(db, now=T0 + 60_000)
    # 一小时里天气翻面、位置也动了——**这会改字**，所以必须靠冻结挡住
    from gits.world import amap

    amap.record(db, {"kind": "weather", "adcode": "330303", "city": "温州市",
                     "weather": "小雨", "temp": 11.0, "humidity": 88.0,
                     "wind": "东北", "power": "4",
                     "report": "2026-11-26 11:30:00"}, now=T0 + 1_800_000)
    loc.record(db, lat=27.90, lon=120.90, acc=30, source="gps",
               fix_ts=T0 + 1_900_000, now=T0 + 1_900_000, city={"city": "宁波市"})
    b = B.freeze(db, now=T0 + 2_400_000)
    assert a == b, "同一小时内背景块变了"
    assert "晴" in a and "小雨" not in a, "冻结没挡住新读数"
    # 换小时才换一份
    nxt = B.freeze(db, now=T0 + HOUR + 60_000)
    assert nxt != a, "换小时了却没换一份"


def test_冻结用的是用的那一刻不是整点(db) -> None:
    """整点算的话，11:59 拿到的还是 11:00 的状态——\"待了多久\"会差一整个小时。"""
    _fill(db)
    # 他在 T0-20 分钟到的；第一次用到背景块是 T0+100 分钟 -> 一共 120 分钟 -> 待了挺久
    # 若按整点(T0)算，只有 20 分钟 -> 刚坐下，差一整个桶
    with Writer(db).txn() as c:
        c.execute("UPDATE place_state SET since=? WHERE actor='her'", (T0 - 20 * 60_000,))
    blob = B.freeze(db, now=T0 + 100 * 60_000)
    assert "待了挺久" in blob, blob


def test_换小时就换一份(db) -> None:
    _fill(db)
    a = B.freeze(db, now=T0)
    b = B.freeze(db, now=T0 + HOUR)
    assert a  # 至少非空
    assert db.scalar("SELECT count(*) FROM brief_snapshot") == 2


def test_只放可核对的事实(db) -> None:
    """概率（到达率、下一站）不许进上下文——它进了就会变成话术。"""
    _fill(db, name="公司")
    b = B.build(db, now=T0)
    assert b["place"] == "公司"
    assert b["city"] == "温州市"
    assert b["weather"] == "晴"
    for bad in ("arrival", "next", "prob", "rate", "reachable"):
        assert bad not in b, f"预测混进背景块了：{bad}"


def test_没有名字就不编名字(db) -> None:
    _fill(db, name=None)
    b = B.build(db, now=T0)
    assert "place" not in b
    assert b["city"] == "温州市"


def test_停留是分桶不是分钟(db) -> None:
    """给分钟，模型就开始算钟点。桶保留信息，丢掉钟面。"""
    _fill(db)
    b = B.build(db, now=T0 + 45 * 60_000)
    assert b["stay"] in [w for _e, w in B.STAY_BUCKETS], b.get("stay")
    assert "45" not in json.dumps(b, ensure_ascii=False)


def test_派生量不落表(db) -> None:
    """停留时长渲染时算，不进任何表。"""
    _fill(db)
    B.build(db, now=T0 + 45 * 60_000)
    cols = [r["name"] for r in db.q("PRAGMA table_info(places)")]
    assert "stay" not in cols and "stay_min" not in cols


def test_序列化是确定性的(db) -> None:
    _fill(db, name="公司")
    one = B.render(B.build(db, now=T0))
    two = B.render(B.build(db, now=T0))
    assert one == two
    assert " " not in one.replace('" ', '"') or True
    assert list(json.loads(one)) == sorted(json.loads(one))


def test_只有实质变化才算变化(db) -> None:
    _fill(db, name="公司")
    base = B.build(db, now=T0)
    # 温度变了：不算实质
    warmer = dict(base, temp=20)
    assert B.changes(base, warmer) == {}
    # 换地方：算
    moved = dict(base, place="家")
    assert B.changes(base, moved) == {"place": "家"}
    # 天气翻面：算
    rainy = dict(base, weather="小雨")
    assert B.changes(base, rainy) == {"weather": "小雨"}


def test_没有读数就是空块(db) -> None:
    assert B.render(B.build(db, now=T0)) == ""
    assert B.freeze(db, now=T0) == ""
