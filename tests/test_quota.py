"""额度账户：每日入账、攒额上限、日上限、周末清零。

用户口径：
  - 每日增加配给额度 → 不会"花完就卡到周一"
  - 限制攒额度 → 攒不成"大事基金"
  - 每日消费上限取三倍配给额度
  - 每周末清零
  - 规则要像电子账户一样可见；消耗时附带进度条
"""

from __future__ import annotations

import dataclasses

import pytest

from gits.context import TurnContext, assemble, PersonaSeed, RelationSnapshot, estimate_tokens
from gits.quota import (
    KINDS, UNIT_PRICE, Account, Activity, Budget, allowance, allowances, bar,
    day_cap, new_week_account,
)

ACTORS = ["her", "peer1", "peer2"]
B = Budget()
A = allowance(B, "her")
DAILY = A.cny
CAP = DAILY * 5          # 攒额上限


def _acct() -> Account:
    return Account(actor="her", balance=DAILY)


# ── 派生 ────────────────────────────────────────────────────
def test_月度是唯一固定量其余派生() -> None:
    am = allowances(Budget(monthly_cny=200.0, autonomy_share=0.30, days_per_month=30), ACTORS)
    assert sum(a.cny for a in am.values()) * 30 == pytest.approx(200.0 * 0.30)


def test_三个实例三等分() -> None:
    vals = [a.cny for a in allowances(Budget(), ACTORS).values()]
    assert len(set(round(v, 9) for v in vals)) == 1


def test_实例数不对就报错() -> None:
    with pytest.raises(ValueError):
        allowances(Budget(actors=3), ["her", "peer1"])


# ── 入账 ────────────────────────────────────────────────────
def test_每天入账一份() -> None:
    a = _acct()
    before = a.balance
    got = a.accrue(DAILY, CAP)
    assert got == pytest.approx(DAILY)
    assert a.balance == pytest.approx(before + DAILY)


def test_攒额有上限() -> None:
    a = Account(actor="her", balance=CAP)
    assert a.accrue(DAILY, CAP) == pytest.approx(0.0)
    assert a.balance == pytest.approx(CAP)


def test_入账会重置今日已花() -> None:
    a = _acct()
    a.charge(DAILY / 2, DAILY)
    assert a.spent_today > 0
    a.accrue(DAILY, CAP)
    assert a.spent_today == 0


def test_花光之后第二天又有() -> None:
    """这是"每日入账"相对"周锅"的唯一实质差别：不会卡到周一。"""
    a = _acct()
    a.charge(a.balance, DAILY)          # 花光
    assert a.balance == pytest.approx(0.0)
    assert a.spendable(DAILY) == pytest.approx(0.0)
    a.accrue(DAILY, CAP)
    assert a.spendable(DAILY) == pytest.approx(DAILY)


def test_周一从入账开始_不会停在零() -> None:
    a = new_week_account("her", DAILY, CAP)
    assert a.balance == pytest.approx(DAILY)
    assert a.spendable(DAILY) > 0


# ── 日上限 ──────────────────────────────────────────────────
def test_日上限是三倍日额度() -> None:
    assert day_cap(B, A) == pytest.approx(DAILY * 3)
    a = Account(actor="her", balance=CAP)       # 攒了很多
    assert a.spendable(DAILY) == pytest.approx(DAILY * 3)


def test_攒多了也花不动() -> None:
    a = Account(actor="her", balance=CAP)
    ok, _ = a.charge(CAP, DAILY)
    assert not ok, "一天不能把攒下的全花掉"
    ok, _ = a.charge(DAILY * 3, DAILY)
    assert ok


def test_超额直接拒绝() -> None:
    a = _acct()
    ok, why = a.charge(DAILY * 3, DAILY)
    assert not ok and "还能花" in why


# ── 周末清零 ────────────────────────────────────────────────
def test_周末清零() -> None:
    a = Account(actor="her", balance=DAILY * 4, spent_week=1.0)
    a.week_reset()
    assert a.balance == 0 and a.spent_week == 0


def test_不花不罚() -> None:
    """不花只是攒着（有上限），不会因为"没用"而扣任何东西。"""
    a = _acct()
    for _ in range(3):
        a.accrue(DAILY, CAP)
    assert a.balance == pytest.approx(min(CAP, DAILY * 4))
    assert a.spent_week == 0


# ── 可见性 ──────────────────────────────────────────────────
def test_账户视图包含全部规则() -> None:
    s = _acct().statement(DAILY)
    for must in ("余额", "今日可花", "每日入账", "攒额上限", "清零"):
        assert must in s, must


def test_进度条单调且占比正确() -> None:
    assert bar(0.0).endswith("0%")
    assert "100%" in bar(1.0)
    assert "50%" in bar(0.5)
    assert len(bar(0.5).split("]")[0]) == len(bar(0.0).split("]")[0])


def test_提示条足够短() -> None:
    """她每次开口都能看见，所以要便宜——十几个 token 的量级。"""
    h = _acct().hint(DAILY)
    assert estimate_tokens(h) <= 20, (h, estimate_tokens(h))


def test_提示条进的是_C_段而不是_A_段() -> None:
    """塞进 A/B 会打断缓存前缀，那是 50 倍价差。"""
    seed = PersonaSeed(narrative="我不太会哄人。", version=1)
    snap = RelationSnapshot(bucket=0, summary="认识 47 天", hubs=(), phase="醒着")
    base = assemble(seed, snap, TurnContext(stimulus="在吗"))
    withq = assemble(seed, snap, TurnContext(stimulus="在吗", quota_hint="额度 [██░] 25% 1.5/2.0 元"))
    assert base.a_hash == withq.a_hash
    assert base.b == withq.b
    assert base.c != withq.c


# ── 不可观测 ────────────────────────────────────────────────
def test_活动记录里没有内容字段() -> None:
    fields = {f.name for f in dataclasses.fields(Activity)}
    assert fields == {"actor", "ts_start", "ts_end", "kind", "cny", "outputs", "note"}
    for banned in ("content", "text", "url", "prompt", "response"):
        assert banned not in fields


def test_单价表覆盖所有额度类型() -> None:
    assert set(UNIT_PRICE) == set(KINDS)
