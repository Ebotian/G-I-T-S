"""主动开口：没有频率闸、没有时机闸，靠压力和预测。

四道"闸"只剩两道事实闸（出口、证据），另外两样是**连续量**：

    压力   憋得越久门槛越低            → 频率自己长出来，不用规则去配
    在场   预测值，按比例削弱价值        → 不做"低于 0.15 就不许说"
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory.graph import add_edge
from gits.speech import initiative as I

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _dangling(db: DB, name: str = "看病", *, times: int = 3,
              start: int = T0 - 6 * HOUR) -> str:
    from gits.core.project import entity_id

    eid = entity_id(name)
    for i in range(times):
        ts = start + i * HOUR
        nid = f"n:{name}-{i}"
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r,"
                      " last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "episode", ts, "world", 1.0, 1.0, ts))
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)
    return eid


def _presence(db: DB) -> None:
    """每天 14-18 点他都说话。每条都标已回，免得变成积压。"""
    with Writer(db).txn() as c:
        for d in range(7):
            for h in (14, 15, 16, 17):
                ts = T0 - d * 86_400_000 + (h - 15) * HOUR
                cur = c.execute("INSERT INTO events(ts, recv_ts, source, kind,"
                                " ext_id, payload, lag, ts_trust, scope)"
                                " VALUES(?,?,?,?,?,?,?,?,?)",
                                (ts, ts, "shell", "msg_in", f"m{d}-{h}",
                                 b"hi", 0, 1, "world"))
                c.execute("INSERT INTO events(ts, recv_ts, source, kind,"
                          " ext_id, ts_trust, scope) VALUES(?,?,?,?,?,?,?)",
                          (ts, ts, "ghost", "replied",
                           f"replied:{cur.lastrowid}", 1, "world"))


def test_没有悬着的事就不说(db) -> None:
    _presence(db)
    got = I.check(db, now=T0)
    assert got["allow"] is False
    assert "悬着" in got["why"]


def test_憋久了就该开口(db) -> None:
    _dangling(db)
    _presence(db)
    got = I.check(db, now=T0)
    assert got["pressure"] == pytest.approx(1.0)
    assert got["allow"] is True, got["why"]


def test_刚说完压力接近零门槛最高(db) -> None:
    """**没有"每 3 小时一次"这种规则。** 压力≈0 → 门槛最高。"""
    _dangling(db)
    _presence(db)
    I.record(db, now=T0 - 60_000, reason="刚说过", candidate="看病")
    got = I.check(db, now=T0)
    assert got["pressure"] < 0.01
    assert "阈值" in got["why"], "应该是价值没超过门槛，不是被规则拦住"


@pytest.mark.xfail(strict=True, reason="已标定：THRESHOLD=0.15 太低，"
                                          "压力降门槛压不住高价值候选——"
                                          "见 docs/research/autonomy.md 待标定")
def test_频率应该由压力蕴含_目前还做不到(db) -> None:
    """**已知缺口，不是通过项。**

    用户的设计是：不加频率闸，靠"憋得越久门槛越低"让频率自己长出来。
    但现在的常数下它长不出来：刚说完（压力≈0，门槛 0.15）时，
    一个强候选的价值仍然是 0.30 左右，照样过线——等于一分钟就能再说一次。

    要让这条成立，需要把 THRESHOLD 抬到 ~0.4（这样压力≈0 时 0.40 挡住 0.30，
    憋满时 0.16 放行）。但那又要用真实数据标定，不能拍脑袋定。
    所以：**先记成 xfail，等有真实交互再定标。**
    """
    _dangling(db)
    _presence(db)
    I.record(db, now=T0 - 60_000, reason="刚说过", candidate="看病")
    assert I.check(db, now=T0)["allow"] is False


def test_他不常在这个点说话_价值被削弱但不是很门槛(db) -> None:
    """在场是**预测**，按比例削弱价值；不是"低于某值一律不许"。"""
    _dangling(db)
    _presence(db)
    night = int(dt.datetime(2026, 11, 27, 3, 0).timestamp() * 1000)
    got = I.check(db, now=night)
    assert got["presence"] is not None and got["presence"] < 0.5
    # 削弱体现在数值上：同样的证据，在夜里价值更低
    day = I.check(db, now=T0)
    assert got["value"] < day["value"], "在场低的时候价值没有被削弱"


def test_连续开口会衰减价值(db) -> None:
    """连续开口的衰减是 gate 里 SEND_DECAY 给的，不是外面加的规则。"""
    _dangling(db)
    _presence(db)
    one = I.check(db, now=T0)
    for i in range(3):
        I.record(db, now=T0 - (i + 1) * 4 * HOUR, reason="连续", candidate=None)
    three = I.check(db, now=T0)
    # 三次连续之后压力已经满了，门槛最低；价值仍应因为 SEND_DECAY 掉下来
    assert three["value"] <= one["value"] * 1.01, (one["value"], three["value"])


def test_判定必须落事件才能计数(db) -> None:
    _dangling(db)
    _presence(db)
    assert I.spoken(db) == []
    I.record(db, now=T0 - 4 * HOUR, reason="说了", candidate="看病")
    assert len(I.spoken(db)) == 1
    assert I.pressure(db, now=T0) < 1.0


def test_出口断了就不开口(db) -> None:
    _dangling(db)
    _presence(db)
    got = I.check(db, now=T0, channel_online=False)
    assert got["allow"] is False
    assert "出口" in got["why"]


def test_没有数据也不当成禁令(db) -> None:
    """空白不是"他一定不在"。没消息的时候在场返回 None，不否决。"""
    assert I.presence(db, now=T0) is None
