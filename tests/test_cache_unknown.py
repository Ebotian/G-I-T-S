"""缓存记账：**"不知道"不等于"没命中"。**

provider 不返回命中字段时，记 0 会让"缓存坏了"和"我们在数一个不存在的数"
长得一模一样——然后 plan() 每两轮换一次窗，一直付全价。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.speech import ctxwin

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


@pytest.fixture()
def win(db):
    return ctxwin.open_window(db, ts=T0)


def test_不知道的轮次不算没命中(db, win) -> None:
    """命中数未知 → 不清零、不+1。**既不假装缓存好，也不假装缓存坏。**"""
    ctxwin.record(db, win, tokens=1000, hit_tokens=None)
    ctxwin.record(db, win, tokens=1000, hit_tokens=None)
    row = ctxwin.current(db)
    assert int(row["zero_streak"]) == 0, "把「不知道」记成了没命中"
    assert ctxwin.hit_rate(db) is None, "没有已知样本却算出了命中率"


def test_不知道不触发换窗(db, win) -> None:
    """以前这里会连着换窗：每两轮付一次全价，账单上看像缓存坏了。"""
    for _ in range(4):
        ctxwin.record(db, win, tokens=1000, hit_tokens=None)
    p = ctxwin.plan(db, now=T0)
    assert p.action != "new_window", p.reason


def test_知道且零命中才算过期(db, win) -> None:
    ctxwin.record(db, win, tokens=1000, hit_tokens=0)
    ctxwin.record(db, win, tokens=1000, hit_tokens=0)
    assert ctxwin.plan(db, now=T0).action == "new_window"


def test_命中率的分母只算知道的(db, win) -> None:
    ctxwin.record(db, win, tokens=1000, hit_tokens=800)
    ctxwin.record(db, win, tokens=1000, hit_tokens=None)     # 不认识
    ctxwin.record(db, win, tokens=1000, hit_tokens=800)
    assert ctxwin.hit_rate(db) == pytest.approx(0.8), "未知轮次混进了分母"


def test_知道的轮次会把连续零打断(db, win) -> None:
    ctxwin.record(db, win, tokens=1000, hit_tokens=0)
    ctxwin.record(db, win, tokens=1000, hit_tokens=500)      # 命中，清零
    ctxwin.record(db, win, tokens=1000, hit_tokens=0)
    assert int(ctxwin.current(db)["zero_streak"]) == 1
