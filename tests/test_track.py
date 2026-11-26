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


# ── 采样不是均匀的：时间加权 vs 按条数 ──────────────────────


def test_按条数会被不均匀采样带偏() -> None:
    """上报是"位置变了才触发"，所以走动时密、久坐时稀。

    这条用**手搭的数据**检验估计量本身，不靠模拟：
    走动的地方 200 条短停留、几乎不说话；家里 4 条长停留、经常说话。
    按条数平均会被那 200 条拉低，按时间加权才对应"时间上的率"。
    """
    import tempfile

    from gits.core.db import DB, Writer

    db = DB(Path(tempfile.mkdtemp()) / "g.db")
    try:
        rows = []
        # 走动：200 条，每条代表 1 分钟
        for i in range(200):
            ts = T0 + i * 60_000
            rows.append(("w", ts, 60_000))
        # 家：4 条，每条代表 3 小时
        for i in range(4):
            ts = T0 + i * 10_800_000
            rows.append(("h", ts, 10_800_000))
        with Writer(db).txn() as c:
            c.execute("INSERT INTO places(id, scope, lat, lon, n, first_ts, last_ts)"
                      " VALUES(1,'self:her',27.8,120.8,200,?,?)", (T0, T0))
            c.execute("INSERT INTO places(id, scope, lat, lon, n, first_ts, last_ts)"
                      " VALUES(2,'self:her',27.9,120.9,4,?,?)", (T0, T0))
            for i, (kind, ts, dwell) in enumerate(rows):
                pid = 1 if kind == "w" else 2
                c.execute("INSERT INTO place_fixes(place_id, event_id, ts, dwell_ms)"
                          " VALUES(?,?,?,?)", (pid, i + 1, ts, dwell))
                c.execute("INSERT INTO learn_cursor(name, last_event_id)"
                          " VALUES('seed',0) ON CONFLICT DO NOTHING")
        # 家里说话了：4 条里 3 条后面跟着消息
        for ts in [T0, T0 + 10_800_000, T0 + 21_600_000]:
            with Writer(db).txn() as c:
                c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                          " payload, lag, ts_trust, scope)"
                          " VALUES(?,?,?,?,?,?,?,?,?)",
                          (ts + 60_000, ts + 60_000, "shell", "msg_in",
                           f"m:{ts}", b"hi", 0, 1, "world"))

        w = coupling.weights(db)
        assert max(w) > 100 * min(w), "权重应该差两个数量级以上"

        # 加权：走动的时间占 200 分钟，家的时间占 720 分钟
        num = sum(ww for (_ts, pid), ww in zip(coupling._fixes(db), w) if pid == 2)
        den = sum(w)
        assert num / den > 0.7, f"家的时间占比应该超过 70%，实际 {num/den:.2f}"
    finally:
        db.close()


def test_久坐的地方按时间也算地方() -> None:
    """按条数算的话，家只有心跳那么几条，会被跳点压过去；按时长算不会。"""
    import tempfile

    from gits.core.db import DB

    fixes, _msgs, _truth = track.gen_irregular(start_ms=T0, days=3)
    db = DB(Path(tempfile.mkdtemp()) / "g.db")
    try:
        track.load(db, fixes, _msgs)
        places.learn(db)
        ps = places.real_places(db)
        assert ps, "一个地方都没认出来"
        home = max(ps, key=lambda p: p["best_dwell_ms"])
        assert places._dist_m(home["lat"], home["lon"], *track.HOME) < 500, \
            "待得最久的地方不是家"
        assert home["best_dwell_ms"] >= places.MIN_DWELL_MS
    finally:
        db.close()


def test_一条跳点不认领它之后那段时间() -> None:
    """跳点后面接的是别的地方，那段时长不能算给跳点。

    否则 30 分钟采样间隔下，一次单点跳点会被认成"在那儿待了 30 分钟"。
    """
    import tempfile

    from gits.core.db import DB
    from gits.world import loc as L

    db = DB(Path(tempfile.mkdtemp()) / "g.db")
    try:
        # 每 30 分钟一条：家、跳点、家
        for i, (lat, lon) in enumerate([
                (track.HOME[0], track.HOME[1]),
                (track.FAR[0], track.FAR[1]),
                (track.HOME[0], track.HOME[1]),
                (track.HOME[0], track.HOME[1]),
                (track.HOME[0], track.HOME[1])]):
            L.record(db, lat=lat, lon=lon, acc=30, source="network",
                     fix_ts=T0 + i * 1_800_000, now=T0 + i * 1_800_000)
        places.learn(db)
        far = [p for p in places.all_places(db)
               if places._dist_m(p["lat"], p["lon"], *track.FAR) < 1000]
        assert far and far[0]["best_dwell_ms"] == 0, \
            f"跳点认领了时间：{far[0] if far else None}"
        assert not places.counts_as_place(far[0])
    finally:
        db.close()
