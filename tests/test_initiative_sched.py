"""主动开口的排期：**间隔由时序模型给，不是常数。**"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech import initiative as I

HOUR = 3_600_000
DAY = 86_400_000
MON = int(dt.datetime(2026, 11, 16, 0, 0).timestamp() * 1000)   # 周一


def _week_of_chats(db: DB) -> None:
    """造一周作息：9-22 点常有消息，0-8 点几乎没有。"""
    random.seed(7)
    for day in range(7):
        for hour in range(24):
            n = random.choice([0, 1, 2, 3]) if 9 <= hour <= 22 else 0
            for k in range(n):
                t = MON + day * DAY + hour * HOUR + k * 60_000
                Writer(db).append_event(
                    source="wx", kind="msg_in", ext_id=f"m{day}-{hour}-{k}",
                    ts=t, recv_ts=t, scope="world", payload=b"hi")


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


@pytest.fixture()
def busy(db):
    _week_of_chats(db)
    return db


def test_夜里比白天看得稀(busy) -> None:
    """这是换掉固定 20 分钟的理由：凌晨三点判多少次，结果都一样。"""
    night = MON + 7 * DAY + 3 * HOUR
    noon = MON + 7 * DAY + 14 * HOUR
    n_ts, _ = I.next_check(busy, now=night, pressure_now=0.3)
    d_ts, _ = I.next_check(busy, now=noon, pressure_now=0.3)
    assert n_ts - night > d_ts - noon, "夜里没有看得更稀"


def test_憋得越久看得越勤(busy) -> None:
    noon = MON + 7 * DAY + 14 * HOUR
    low, _ = I.next_check(busy, now=noon, pressure_now=0.1)
    high, _ = I.next_check(busy, now=noon, pressure_now=0.9)
    assert high - noon < low - noon


def test_他在的时候最勤(busy) -> None:
    """压力满 + 他在写消息 → 十几分钟量级。"""
    noon = MON + 7 * DAY + 14 * HOUR
    ts, _ = I.next_check(busy, now=noon, pressure_now=1.0)
    assert ts - noon <= 30 * 60_000, f"他在的时候等了 {(ts - noon) // 60_000} 分钟"


def test_他快回来了就把检查挪过去(busy) -> None:
    """睡着但一小时后起床：没必要干等四小时。"""
    night = MON + 7 * DAY + 6 * HOUR           # 6 点，还在睡
    ts, why = I.next_check(busy, now=night, pressure_now=0.3)
    assert "回来" in why, why
    # 落在整点：他把"什么时候看"对齐到小时边界，不是随手一拍
    assert (ts - night) % HOUR == 0, why
    # 而且比干等上限要早——否则这条逻辑等于没写
    assert ts - night < I.MAX_GAP_MS, why


def test_间隔有上下限(busy) -> None:
    for h in range(24):
        now = MON + 7 * DAY + h * HOUR
        ts, _ = I.next_check(busy, now=now, pressure_now=0.5)
        assert 60_000 <= ts - now <= I.MAX_GAP_MS + HOUR


def test_没有数据时退到先验_而且不崩(db) -> None:
    ts, why = I.next_check(db, now=MON, pressure_now=0.5)
    assert ts > MON and why


def test_同样的输入给同样的结果(busy) -> None:
    """**排期不能有随机性。**决定落事件，排期要能解释。"""
    now = MON + 7 * DAY + 10 * HOUR
    a, _ = I.next_check(busy, now=now, pressure_now=0.4)
    b, _ = I.next_check(busy, now=now, pressure_now=0.4)
    assert a == b


def test_额度花光了就不开口(db) -> None:
    """她自己的钱花完了——不开口不是坏了，是今天花完了。"""
    from gits.io import ledger

    s = ledger.state(db, actor="her", now=MON)
    ledger.charge(db, actor="her", kind="llm", cost=s["day_cap"], now=MON)
    got = I.check(db, now=MON)
    assert got["allow"] is False
    assert "额度" in got["why"]


def test_数据太薄时不装作知道(db) -> None:
    """一天数据的小时先验是阶跃：19 点说过话，3 点就成了"他从不来"。
    那是没数据，不是不在——退回中性，不要凭空造作息。"""
    from gits.core.db import Writer

    # 只有一天、只有一个小时有过消息
    base = MON + 5 * DAY + 19 * HOUR
    Writer(db).append_event(source="wx", kind="msg_in", ext_id="only",
                            ts=base, recv_ts=base, scope="world",
                            payload=b"hi")
    night = MON + 6 * DAY + 3 * HOUR
    day = MON + 6 * DAY + 14 * HOUR
    n_ts, n_why = I.next_check(db, now=night, pressure_now=0.3)
    d_ts, d_why = I.next_check(db, now=day, pressure_now=0.3)
    assert n_ts - night == d_ts - day, f"薄数据下夜里和白天不该差这么多：{n_why} / {d_why}"
    # 中性先验给的是小时量级；若拿那一天的 0/1 阶跃当作息，会直接顶到 4 小时上限
    assert n_ts - night < I.MAX_GAP_MS, n_why
    assert "回来" not in n_why, f"薄数据下不该找回来时刻：{n_why}"


def test_数据够了才按作息排(db) -> None:
    """同样的钟点，数据够了之后夜里明显更稀。"""
    from gits.core.db import Writer

    base = MON + 5 * DAY + 19 * HOUR
    Writer(db).append_event(source="wx", kind="msg_in", ext_id="only",
                            ts=base, recv_ts=base, scope="world",
                            payload=b"hi")
    thin_night = MON + 6 * DAY + 3 * HOUR
    before, _ = I.next_check(db, now=thin_night, pressure_now=0.3)

    _week_of_chats(db)                      # 补到 7 天
    fat_night = MON + 14 * DAY + 3 * HOUR
    after, why = I.next_check(db, now=fat_night, pressure_now=0.3)
    assert after - fat_night > 2 * HOUR, why


def test_薄数据下也不猜他几点回来(db) -> None:
    """这一天的"作息"就是一个点。拿它去预测"他 19 点回来"是编的。"""
    from gits.core.db import Writer

    base = MON + 5 * DAY + 19 * HOUR
    Writer(db).append_event(source="wx", kind="msg_in", ext_id="only",
                            ts=base, recv_ts=base, scope="world",
                            payload=b"hi")
    now = MON + 6 * DAY + 17 * HOUR + 30 * 60_000
    ts, why = I.next_check(db, now=now, pressure_now=0.0)
    assert "回来" not in why, f"薄数据下猜了他回来的时刻：{why}"
    assert ts - now <= I.MAX_GAP_MS
