"""额度账本：**入账是规则，支出是事件。**"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.io import ledger

# 2026-11-23 是周一
MON = int(dt.datetime(2026, 11, 23, 0, 0).timestamp() * 1000)
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_周一先入账再花钱(db) -> None:
    """不会出现"余额 0 还要等到明天"。"""
    s = ledger.state(db, actor="her", now=MON)
    assert s["balance"] == pytest.approx(s["daily"])
    assert s["spendable"] == pytest.approx(s["daily"])


def test_每天入账_封顶(db) -> None:
    """攒额上限：攒不下"大事基金"。"""
    s0 = ledger.state(db, actor="her", now=MON)
    s5 = ledger.state(db, actor="her", now=MON + 5 * DAY)
    s6 = ledger.state(db, actor="her", now=MON + 6 * DAY)
    assert s5["balance"] == pytest.approx(s0["accrue_cap"]), "五天攒到了上限"
    assert s6["balance"] == pytest.approx(s5["balance"]), "第六天还在涨，没封顶"


def test_周日之后清零(db) -> None:
    """周日 24:00 = 周一 00:00。周一从一天的额度重新开始，不继承上周。"""
    ledger.charge(db, actor="her", kind="llm", cost=0.5, now=MON + 3 * DAY)
    before = ledger.state(db, actor="her", now=MON + 6 * DAY)
    after = ledger.state(db, actor="her", now=MON + 7 * DAY)
    assert before["spent_week"] > 0
    assert after["spent_week"] == 0
    assert after["balance"] == pytest.approx(after["daily"])


def test_日上限拦得住(db) -> None:
    """攒了五天不等于一天能全花光。"""
    s = ledger.state(db, actor="her", now=MON + 4 * DAY)
    daily, cap = s["daily"], s["day_cap"]
    assert cap == pytest.approx(daily * 3.0)
    ledger.charge(db, actor="her", kind="llm", cost=cap, now=MON + 4 * DAY)
    ok, why = ledger.can_spend(db, cost=0.01, actor="her", now=MON + 4 * DAY)
    assert ok is False and "额度不够" in why


def test_超支也照记(db) -> None:
    """真花了钱就得记账。账本不能因为"超了"假装没花。"""
    s = ledger.state(db, actor="her", now=MON)
    ledger.charge(db, actor="her", kind="llm", cost=s["day_cap"] * 2, now=MON)
    got = ledger.state(db, actor="her", now=MON)
    assert got["spent_today"] > s["day_cap"]
    assert got["spendable"] == 0


def test_记账是事件_重放能重建(db) -> None:
    ledger.charge(db, actor="her", kind="llm", cost=0.2, now=MON + 3600_000)
    before = ledger.state(db, actor="her", now=MON + 7200_000)
    assert db.scalar("SELECT count(*) FROM events WHERE kind='spend'") == 1
    after = ledger.state(db, actor="her", now=MON + 7200_000)
    assert before == after


def test_同一笔只记一次(db) -> None:
    """稳定身份：重试不会把一次调用记成两次。"""
    ledger.charge(db, actor="her", kind="llm", cost=0.2, now=MON, ref="reply:9")
    ledger.charge(db, actor="her", kind="llm", cost=0.2, now=MON, ref="reply:9")
    assert db.scalar("SELECT count(*) FROM events WHERE kind='spend'") == 1


def test_支出里没有内容(db) -> None:
    """内容不可见：账本只记元数据。"""
    ledger.charge(db, actor="her", kind="llm", cost=0.2, now=MON,
                  what="reply")
    raw = db.scalar("SELECT payload FROM events WHERE kind='spend'")
    obj = json.loads(raw)
    assert set(obj) == {"actor", "kind", "cost", "what", "units"}


def test_按usage记账不是估算(db) -> None:
    """账单以 API 返回的 usage 为准。"""
    usage = {"prompt_cache_hit_tokens": 1000, "prompt_cache_miss_tokens": 1000,
             "completion_tokens": 500}
    cost, _ev = ledger.charge_usage(db, actor="her", usage=usage, now=MON,
                                    what="reply")
    assert cost == pytest.approx(1000 / 1e6 + 1000 / 1e6 + 500 * 4 / 1e6)


def test_月预算可以覆盖(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITS_MONTHLY_CNY", "600")
    d = DB(tmp_path / "b.db")
    s = ledger.state(d, actor="her", now=MON)
    assert s["daily"] == pytest.approx(0.667 * 3, rel=0.01)
    d.close()
