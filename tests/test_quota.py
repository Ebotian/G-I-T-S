"""配额：月度固定 → 每日额度 → **周额度 + 日上限**（周清日结转）。

口径：
  - 月度是唯一固定量，其余派生；三个实例三等分
  - 周内没花完的自然留在周锅里（结转）；周一清零
  - **日上限**防止一天花光整周；**钱是唯一硬约束**，分项只做提示
  - 不花不罚：没花完只是留着
  - 活动记录只有元数据，没有内容
"""

from __future__ import annotations

import dataclasses

import pytest

from gits.quota import (
    KINDS, UNIT_PRICE, Activity, Budget, Ledger, allowance, allowances,
    autonomy_pool, day_cap, new_week,
)

ACTORS = ["her", "peer1", "peer2"]
B = Budget()
A = allowance(B, "her")
FETCH = UNIT_PRICE["fetch"]


# ── 派生 ────────────────────────────────────────────────────
def test_月度是唯一固定量其余派生() -> None:
    am = allowances(Budget(monthly_cny=200.0, autonomy_share=0.30, days_per_month=30), ACTORS)
    assert sum(a.cny for a in am.values()) * 30 == pytest.approx(200.0 * 0.30)


def test_三个实例三等分() -> None:
    vals = [a.cny for a in allowances(Budget(), ACTORS).values()]
    assert len(set(round(v, 9) for v in vals)) == 1


def test_改月预算整体等比缩放() -> None:
    a = allowances(Budget(monthly_cny=200.0), ACTORS)["her"]
    b = allowances(Budget(monthly_cny=400.0), ACTORS)["her"]
    assert b.cny == pytest.approx(a.cny * 2)


def test_实例数不对就报错() -> None:
    with pytest.raises(ValueError):
        allowances(Budget(actors=3), ["her", "peer1"])


def test_本地算力按时间三等分() -> None:
    am = allowances(Budget(exec_minutes_per_day=21), ACTORS)
    assert am["her"].units["exec_sec"] == pytest.approx(21 * 60 / 3)


# ── 周额度 + 日上限 ────────────────────────────────────────
def test_周额度是日额度乘七天() -> None:
    L = new_week("W1", "mon")
    assert L.weekly_budget(A, B) == pytest.approx(A.cny * 7)


def test_日上限封顶() -> None:
    """一周额度很大，但一天只能花 day_cap——否则"周额度"就等于"当日额度"。"""
    L = new_week("W1", "mon")
    assert L.limit_cny("her", A, B) == pytest.approx(day_cap(B, A))
    assert day_cap(B, A) < A.cny * 7


def test_没花完的自然留在周锅里() -> None:
    L = new_week("W1", "mon")
    assert L.carry_out("her", A, B) == pytest.approx(day_cap(B, A))
    nxt = L.tomorrow()                       # 今天一分没花
    assert nxt.spent_before.get("her", 0.0) == 0
    assert nxt.limit_cny("her", A, B) == pytest.approx(day_cap(B, A))


def test_连着花满也不会超周额度() -> None:
    L = new_week("W1", "mon")
    total = 0.0
    for _ in range(7):
        ok, _why = L.charge("her", "fetch", L.limit_cny("her", A, B) / FETCH, A, B)
        total += L.spent_cny("her")
        L = L.tomorrow()
    assert total <= A.cny * 7 + 1e-9
    assert total > A.cny * 3                  # 确实花掉了不少


def test_花光之后当周就没有了() -> None:
    L = new_week("W1", "mon")
    for _ in range(3):
        L.charge("her", "fetch", L.limit_cny("her", A, B) / FETCH, A, B)
        L = L.tomorrow()
    assert L.limit_cny("her", A, B) == pytest.approx(0.0, abs=1e-9)
    ok, why = L.charge("her", "fetch", 1, A, B)
    assert not ok and "只剩" in why


def test_周一清零() -> None:
    L = new_week("W1", "mon")
    L.charge("her", "fetch", L.limit_cny("her", A, B) / FETCH, A, B)
    assert L.tomorrow().spent_before["her"] > 0
    assert new_week("W2", "mon").spent_before == {}


def test_不花不罚() -> None:
    """没花完只是留着，不会因为"没用"而减少后面的额度。"""
    idle = new_week("W1", "mon")
    for _ in range(3):
        idle = idle.tomorrow()
    assert idle.limit_cny("her", A, B) == pytest.approx(day_cap(B, A))
    assert idle.spent_before.get("her", 0.0) == 0


# ── 硬约束只有钱 ────────────────────────────────────────────
def test_分项只做提示_超了也能花() -> None:
    """今天想多读点、少写点，是她的自由；按分项卡死等于替她决定用法。"""
    L = new_week("W1", "mon")
    many = A.units["fetch"] * 3
    ok, _why = L.charge("her", "fetch", many, A, B)
    assert ok


def test_超额直接拒绝() -> None:
    L = new_week("W1", "mon")
    ok, why = L.charge("her", "fetch", day_cap(B, A) / FETCH * 2, A, B)
    assert not ok and "只剩" in why


def test_各实例的账互不影响() -> None:
    am = allowances(Budget(), ACTORS)
    L = new_week("W1", "mon")
    L.charge("her", "fetch", L.limit_cny("her", am["her"], B) / FETCH, am["her"], B)
    assert L.spent_cny("her") > 0
    assert L.spent_cny("peer1") == 0
    assert L.limit_cny("peer1", am["peer1"], B) == pytest.approx(day_cap(B, am["peer1"]))


# ── 不可观测 ────────────────────────────────────────────────
def test_活动记录里没有内容字段() -> None:
    fields = {f.name for f in dataclasses.fields(Activity)}
    assert fields == {"actor", "ts_start", "ts_end", "kind", "cny", "outputs", "note"}
    for banned in ("content", "text", "url", "prompt", "response"):
        assert banned not in fields


def test_单价表覆盖所有额度类型() -> None:
    assert set(UNIT_PRICE) == set(KINDS)
