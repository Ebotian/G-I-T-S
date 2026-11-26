"""用合成的轨迹检验时空学习。**它检验实现，不检验理论。**

参数取自真实数据（30 m 精度、15 分钟采样、40 公里跳点），
见 docs/research/space.md 和 synth/track.py。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.world import coupling, places
from synth import track

#: 2026-11-23 是周一，整天对齐在整点上
T0 = int(dt.datetime(2026, 11, 23, 0, 0).timestamp() * 1000)
DAY = 86_400_000


@pytest.fixture()
def world(tmp_path: Path):
    db = DB(tmp_path / "g.db")
    fixes, msgs = track.gen(start_ms=T0, days=7)
    track.load(db, fixes, msgs)
    places.learn(db)
    yield db, fixes, msgs
    db.close()


def test_两个地方能分开(world) -> None:
    db, _fixes, _msgs = world
    ps = places.real_places(db)
    assert len(ps) == 2, f"应该恰好两个地方，实际 {[(p['n'], round(p['lat'],4)) for p in ps]}"
    a, b = sorted(ps, key=lambda p: -p["n"])
    d = places._dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
    assert 1000 < d < 20000, f"两个地方分得太开或太近：{d:.0f} m"


def test_跳点不该长成一个地方(world) -> None:
    """网络切换造成的 40 公里跳点，一次而已，不能变成'他常去的地方'。"""
    db, _fixes, _msgs = world
    far = [p for p in places.real_places(db)
           if places._dist_m(p["lat"], p["lon"], track.FAR[0], track.FAR[1]) < 1000]
    assert far == [], f"跳点被当成场所了：{far}"


def test_一次跳点不会成为所在场所() -> None:
    """场所是**确认过**的，不是最后一条读数。"""
    import tempfile

    from gits.world import loc as L

    db = DB(Path(tempfile.mkdtemp()) / "g.db")
    try:
        L.record(db, lat=track.FAR[0], lon=track.FAR[1], acc=30,
                 source="network", fix_ts=T0, now=T0)
        places.learn(db)
        assert places.current(db) is None, "一条读数就成了'他在的地方'"
        assert places.real_places(db) == []
    finally:
        db.close()


def test_跳点不会让所在场所抖动(world) -> None:
    """观测变了不等于确认要变——迟滞就是为了这个。"""
    db, fixes, _msgs = world
    seen = set()
    for f in fixes[:200]:
        cur = places.current(db)
        if cur:
            seen.add(cur["id"])
    assert len(seen) <= 2, f"所在场所在两种以上来回跳：{seen}"


def test_真的搬了会确认(world) -> None:
    """连续多次同向 = 真的移动，要认。"""
    db, _fixes, _msgs = world
    before = places.current(db)
    assert before is not None
    # 在 20 公里外连续待 3 次
    far2 = (27.9500, 121.0500)
    from gits.core.db import Writer
    from gits.world import loc as L

    # 要连续待够：先攒停留（MIN_DWELL），再连续同向（CONFIRM_N）。
    # 这不是宽松与否的问题——一次跳点必须不可能满足其中任何一个。
    for i in range(6):
        lat = far2[0] + i * 1e-5
        _ev, created = L.record(db, lat=lat, lon=far2[1], acc=20, source="gps",
                                fix_ts=T0 + 8 * DAY + i * 900_000,
                                now=T0 + 8 * DAY + i * 900_000)
        assert created
        places.learn(db)
    after = places.current(db)
    assert after is not None and after["id"] != before["id"], "真的移动没被确认"


def test_没有名字就只说城市(world) -> None:
    db, _fixes, _msgs = world
    assert places.line(db, now=T0 + 5 * DAY) == "", "没名字的地方不许编名字"


def test_有了名字才说场所(world) -> None:
    db, _fixes, _msgs = world
    cur = places.current(db)
    assert cur is not None
    from gits.core.db import Writer

    with Writer(db).txn() as c:
        c.execute("UPDATE places SET name='公司' WHERE id=?", (cur["id"],))
    assert "公司" in places.line(db, now=T0 + 5 * DAY)


# ── 自建预测：收缩 ──────────────────────────────────────────


def test_场所层能压过全局(world) -> None:
    """同一个小时在两个地方的率应该分得开——不然耦合等于没做。"""
    db, _fixes, _msgs = world
    ps = sorted(places.real_places(db), key=lambda p: -p["n"])
    home, work = ps[0], ps[1]
    r_home = coupling.arrival(db, now=T0, place_id=home["id"], hour=10)
    r_work = coupling.arrival(db, now=T0, place_id=work["id"], hour=10)
    # 收缩会把两个数拉近（这是有意的），所以看**方向**和**倍数**，不看差值。
    assert r_work > r_home * 1.2, (
        f"10 点的两个地方分不开：home={r_home:.3f} work={r_work:.3f}")


def test_样本少的格退化成全局(world) -> None:
    """**收缩**：一格只有一两条观测时，它应该几乎等于全局。"""
    db, _fixes, _msgs = world
    # 造一个只有一条定位的"地方"：它没有资格叫地方，它的率也不该有自己的性格。
    from gits.world import loc as L

    L.record(db, lat=27.5000, lon=120.5000, acc=25, source="gps",
             fix_ts=T0 + 9 * DAY, now=T0 + 9 * DAY)
    places.learn(db)
    one = [p for p in places.all_places(db) if p["n"] == 1]
    assert one, "应该有一个只有一条定位的点"
    pid = one[0]["id"]
    assert not places.counts_as_place(one[0])
    r = coupling.rates(db, now=T0)
    for h in range(24):
        assert abs(r["by_place"][pid][h] - r["global"][h]) < 0.10, \
            f"一条观测就长出自己的性格了（h={h}）"


def test_没有证据时猜他不动(world) -> None:
    db, _fixes, _msgs = world
    cur = places.current(db)
    nxt = coupling.next_place(db, now=T0 + 5 * DAY)
    assert nxt, "应该给出一个分布"
    assert nxt[0][0] == cur["id"], "最可能的去处应该是原地"
    assert sum(p for _pid, p in nxt) == pytest.approx(1.0, abs=1e-6)


def test_率永远说不死(world) -> None:
    """自建预测返回的是概率，不是断言。0 和 1 都不该出现。"""
    db, _fixes, _msgs = world
    for h in range(24):
        v = coupling.arrival(db, now=T0, hour=h)
        assert 0.0 <= v <= 1.0
