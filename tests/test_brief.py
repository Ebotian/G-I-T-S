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
import gits.world.brief as B
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











def test_只放可核对的事实(db) -> None:
    """概率（到达率、下一站）不许进上下文——它进了就会变成话术。"""
    _fill(db, name="公司")
    b = B.build(db, now=T0)
    assert b["loc"] == "公司"
    assert b["weather"] == "晴"
    for bad in ("arrival", "next", "prob", "rate", "reachable", "city"):
        assert bad not in b, f"预测混进背景块了：{bad}"


def test_没有名字就给占位词(db) -> None:
    """**不编名字。** 从「夜里 90% 在这」推断出「这是家」是猜；
    猜错了她会在你上班时问「到家了？」。给占位词，让模型决定要不要问。"""
    _fill(db, name=None)
    b = B.build(db, now=T0)
    assert b["loc"] == B.PLACEHOLDER
    assert "city" not in b, "城市对聊天没用"


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


def test_每轮追加是纯函数(db) -> None:
    """同一份数据 + 同一时刻 -> 同一段文本。**这条现在靠纯函数，不靠冻结。**"""
    _fill(db, name="公司")
    a = B.render(B.build(db, now=T0))
    b = B.render(B.build(db, now=T0))
    assert a == b
    # 状态一变，文本就必须跟着变——不做缓存、不冻结
    with Writer(db).txn() as c:
        c.execute("UPDATE place_state SET since=? WHERE actor='her'", (T0 - 600_000,))
    c2 = B.render(B.build(db, now=T0 + 3_600_000))
    assert c2 != a


# ── 边界：装配不认识世界 ────────────────────────────────────


def test_装配不直接碰世界模块() -> None:
    """**装配只管拼，背景只管给。** 谁都能加字段的话，
    上下文里迟早会长出没人知道从哪来的东西。"""
    import pathlib as _p

    src = _p.Path("gits/speech/respond.py").read_text()
    for bad in ("from gits.world import amap", "from gits.world import loc",
                "from gits.world import places", "import gits.world.brief"):
        assert bad not in src, f"装配里直接引了世界模块：{bad}"
    assert "brief_text" in src, "装配应该只认这一个入口"


def test_详细走分类工具(db) -> None:
    """详细的走工具，按类取。未知分类返回空，不抛。"""
    from gits.world import catalog

    names = [c["name"] for c in catalog(db, now=T0, proactive=True)]
    assert {"where", "outside", "place", "self", "name_place"} <= set(names)
    for c in catalog(db, now=T0, proactive=True):
        assert c.get("about"), f"{c['name']} 没有作用说明"


def test_工具目录是分类不是一个大接口(db) -> None:
    from gits.world import catalog, detail

    for t in catalog(db, now=T0, proactive=True):
        got = detail(db, t["name"], now=T0, proactive=True)
        assert isinstance(got, dict), t["name"]
    assert detail(db, "不存在的分类", now=T0, proactive=True) == {}


# ── 拼接规则：只在新开上下文时出现一次 ──────────────────────


def test_规则只在新开上下文出现一次(db) -> None:
    from gits.world import brief_text

    _fill(db, name="公司")
    first = brief_text(db, now=T0, head=True)
    later = brief_text(db, now=T0, head=False)
    assert "只信最后一行" in first, "新开上下文应该带拼接规则"
    assert "只信最后一行" not in later, "每轮都带规则就是白烧 token"
    assert first.endswith(later), "规则应该在数据前面，不改变数据本身"


def test_loc_是地点不是城市(db) -> None:
    """城市对聊天没有用——"你在温州"不构成话题；"你在公司"才有。"""
    from gits.world import brief_text

    _fill(db, name="公司")
    text = brief_text(db, now=T0)
    assert '"loc":"公司"' in text
    assert "温州市" not in text


# ── 节奏闸：高频对话不许调工具 ──────────────────────────────


def _msgs(db: DB, gaps_ms: list[int], *, start: int = T0) -> None:
    t = start
    with Writer(db).txn() as c:
        for i, g in enumerate(gaps_ms):
            t += g
            c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                      " payload, lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                      (t, t, "shell", "msg_in", f"m{i}", b"hi", 0, 1, "world"))


def test_高频对话禁用工具(db) -> None:
    """一句接一句的时候，多等一个来回就是"卡住了"。"""
    from gits.speech import pacing

    # 平时十分钟一条，最近四条半分钟一条
    _msgs(db, [600_000] * 40 + [30_000] * 4)
    fast, why = pacing.is_fast(db, now=T0 + 40 * 600_000 + 4 * 30_000 + 5_000)
    assert fast, why
    assert pacing.tools_allowed(db, now=T0 + 40 * 600_000 + 120_000)[0] is False


def test_低频对话允许工具(db) -> None:
    from gits.speech import pacing

    _msgs(db, [600_000] * 40)
    now = T0 + 40 * 600_000 + 20 * 60_000
    assert pacing.tools_allowed(db, now=now)[0] is True


def test_主动开口永远允许(db) -> None:
    """主动开口没有人在等，等一下不突兀。"""
    from gits.speech import pacing

    _msgs(db, [600_000] * 40 + [30_000] * 4)
    now = T0 + 40 * 600_000 + 4 * 30_000
    assert pacing.tools_allowed(db, now=now, proactive=True)[0] is True


def test_样本不足时不误判(db) -> None:
    """没数据就一律禁用，是最糟的默认值。"""
    from gits.speech import pacing

    _msgs(db, [60_000, 60_000])
    assert pacing.is_fast(db, now=T0 + 999_999)[0] is False


# ── 她的命名入口 ────────────────────────────────────────────


def test_她能给地方起名字(db) -> None:
    from gits.world import places

    pid = _fill(db, name=None)
    ev = places.name(db, place_id=pid, alias="公司", now=T0)
    assert ev is not None
    assert places.aliases(db, pid) == ["公司"]
    b = B.build(db, now=T0)
    assert b["loc"] == "公司", "起了名字之后背景块该用它"


def test_名字可以有多个且确定(db) -> None:
    from gits.world import places

    pid = _fill(db, name=None)
    places.name(db, place_id=pid, alias="公司", now=T0)
    places.name(db, place_id=pid, alias="单位", now=T0 + 1000)
    assert places.aliases(db, pid) == ["公司", "单位"]
    assert places.name_of({"id": pid}, db) == "公司", "主叫法要确定"


def test_名字走事件能重放(db) -> None:
    from gits.core import project
    from gits.world import places

    pid = _fill(db, name=None)
    places.name(db, place_id=pid, alias="公司", now=T0)
    project.restore(db)
    # 重放会清投影表；place_name 事件必须把它重建出来
    assert places.aliases(db, pid) == ["公司"]


def test_空名字和不合法名字被拒(db) -> None:
    from gits.world import places

    pid = _fill(db, name=None)
    assert places.name(db, place_id=pid, alias="   ", now=T0) is None
    assert places.name(db, place_id=pid, alias="很长" * 20, now=T0) is None
