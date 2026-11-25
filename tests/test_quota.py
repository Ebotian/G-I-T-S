"""配额：月度固定预算 → 每个个体每天的上限。

口径（DESIGN §18.6）：
  - 月度是唯一的固定量，其余全部派生
  - 三个实例**三等分**自主活动额度（平等要落到钱上）
  - 日清不结转：结转会让她攒额度等大事，然后为了"用掉"而行动
  - 超额直接拒绝，不是先用了再说
  - 活动记录**只有元数据**，没有内容
"""

from __future__ import annotations

import dataclasses

import pytest

from gits.quota import (
    KINDS, UNIT_PRICE, Activity, Allowance, Budget, Ledger, allowance,
    allowances, autonomy_pool,
)

ACTORS = ["her", "peer1", "peer2"]


def test_月度是唯一固定量其余派生() -> None:
    b = Budget(monthly_cny=200.0, autonomy_share=0.30, days_per_month=30)
    am = allowances(b, ACTORS)
    total = sum(a.cny for a in am.values()) * b.days_per_month
    assert total == pytest.approx(200.0 * 0.30)


def test_三个实例三等分() -> None:
    am = allowances(Budget(), ACTORS)
    vals = [a.cny for a in am.values()]
    assert len(set(round(v, 9) for v in vals)) == 1


def test_改月预算整体等比缩放() -> None:
    a = allowances(Budget(monthly_cny=200.0), ACTORS)["her"]
    b = allowances(Budget(monthly_cny=400.0), ACTORS)["her"]
    assert b.cny == pytest.approx(a.cny * 2)
    assert b.units["fetch"] == pytest.approx(a.units["fetch"] * 2)


def test_实例数不对就报错() -> None:
    with pytest.raises(ValueError):
        allowances(Budget(actors=3), ["her", "peer1"])


def test_本地算力按时间三等分() -> None:
    b = Budget(exec_minutes_per_day=21)
    am = allowances(b, ACTORS)
    assert am["her"].units["exec_sec"] == pytest.approx(21 * 60 / 3)


# ── 账本 ────────────────────────────────────────────────────
def test_超额直接拒绝() -> None:
    a = allowance(Budget(), "her")
    L = Ledger(day="d")
    ok, _ = L.charge("her", "fetch", a.units["fetch"] - 1, a)
    assert ok
    ok, why = L.charge("her", "fetch", 5, a)
    assert not ok and "不够" in why


def test_各实例的账互不影响() -> None:
    am = allowances(Budget(), ACTORS)
    L = Ledger(day="d")
    L.charge("her", "fetch", 10, am["her"])
    assert L.remaining("peer1", "fetch", am["peer1"]) == pytest.approx(am["peer1"].units["fetch"])


def test_花费按同一张单价表换算() -> None:
    """额度派生与花费核算必须用同一张表，否则对不上账。"""
    a = allowance(Budget(), "her")
    L = Ledger(day="d")
    L.charge("her", "fetch", 10, a)
    assert L.spent_cny() == pytest.approx(10 * UNIT_PRICE["fetch"])


def test_总额是硬的_分项是软的() -> None:
    """每个 kind 各给一份"只花这一类能花多少"，加起来必然超过日额度。
    真正卡住的是**这个人的日额度**——输入输出比例事先是未知的。"""
    a = allowance(Budget(), "her")
    L = Ledger(day="d")
    for k in ("llm_in", "llm_out", "fetch", "search"):
        L.charge("her", k, a.units[k], a)          # 前几项可能成功
    assert L.spent_cny("her") <= a.cny + 1e-9

    # 再要一次就一定被总额挡下
    ok, why = L.charge("her", "fetch", 1, a)
    assert not ok


def test_总额按人分开算() -> None:
    am = allowances(Budget(), ACTORS)
    L = Ledger(day="d")
    L.charge("her", "fetch", am["her"].units["fetch"], am["her"])
    assert L.spent_cny("her") > 0
    assert L.spent_cny("peer1") == 0


def test_日清不结转() -> None:
    """今天的余额不会出现在明天——否则她会攒着等"大事"。"""
    a = allowance(Budget(), "her")
    d1 = Ledger(day="2026-11-25")
    d1.charge("her", "fetch", 5, a)
    d2 = Ledger(day="2026-11-26")
    assert d2.remaining("her", "fetch", a) == pytest.approx(a.units["fetch"])


# ── 不可观测 ────────────────────────────────────────────────
def test_活动记录里没有内容字段() -> None:
    """她读了什么、写了什么，不进这张表——也就不进 status、不进日志。"""
    fields = {f.name for f in dataclasses.fields(Activity)}
    assert fields == {"actor", "ts_start", "ts_end", "kind", "cny", "outputs", "note"}
    for banned in ("content", "text", "url", "prompt", "response"):
        assert banned not in fields


def test_允许的_activity_kind_是开放的() -> None:
    """形式自由：不是固定几个功能，而是"她这段时间做了什么类型的事"。"""
    kinds = {"browse", "read", "write", "script", "experiment", "peer", "idle"}
    mk = lambda k: Activity(actor="her", ts_start=0, ts_end=1, kind=k, cny=0, outputs=0)
    for k in kinds:
        assert mk(k).kind == k


def test_单价表覆盖所有额度类型() -> None:
    assert set(UNIT_PRICE) == set(KINDS)
