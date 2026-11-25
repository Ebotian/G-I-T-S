"""M2：开口决策（纯函数）+ 互惠基线。

合成数据的用法：造两条事件流，一条是"她说"，一条是"他回"，
然后看决策函数会不会在该沉默的时候沉默。
"""

from __future__ import annotations

import random

import pytest

from gits.gate import (
    RECIPROCITY_FALLBACK, SEND_DECAY, GateInputs, elicitation_ratio, evaluate,
    open_value, should_swallow,
)

NOW = 1_800_000_000_000
MIN = 60_000


def _good(**kw) -> GateInputs:
    base = dict(unexcluded=0.8, resolvable=0.7, reciprocity=0.6, consecutive=0)
    base.update(kw)
    return GateInputs(**base)


# ── 价值函数 ────────────────────────────────────────────────
def test_任一项为零就不开口() -> None:
    for kw in ({"unexcluded": 0.0}, {"resolvable": 0.0},
               {"backlog_pressure": 1.0}, {"reciprocity": 0.0}):
        r = evaluate(_good(**kw))
        assert not r.allow, kw
        assert r.reason


def test_连续开口会衰减() -> None:
    v0 = open_value(_good(consecutive=0))
    v3 = open_value(_good(consecutive=3))
    assert v3 == pytest.approx(v0 * SEND_DECAY ** 3)
    assert v3 < v0


def test_积压重时不撩人() -> None:
    assert open_value(_good(backlog_pressure=0.9)) < open_value(_good(backlog_pressure=0.1))


def test_承诺线优先级最高() -> None:
    r = evaluate(_good(promise_violated=True, unexcluded=1.0, resolvable=1.0))
    assert not r.allow
    assert "承诺线" in r.reason


def test_没有出口就不开口() -> None:
    r = evaluate(_good(channel_online=False))
    assert not r.allow and "出口" in r.reason


def test_互惠系数缺失时用兜底且标注() -> None:
    i = _good(reciprocity=None)
    assert open_value(i) == pytest.approx(open_value(_good(reciprocity=RECIPROCITY_FALLBACK)))
    assert "兜底" in evaluate(i).reason


def test_每个决策都带理由() -> None:
    cases = [
        _good(), _good(unexcluded=0.0), _good(phase_allows=False),
        _good(promise_violated=True), _good(backlog_pressure=1.0),
        _good(unexcluded=0.01, resolvable=0.01),
    ]
    for c in cases:
        assert evaluate(c).reason


def test_吞掉是显式结果() -> None:
    assert should_swallow(_good(unexcluded=0.0))
    assert not should_swallow(_good())


# ── 互惠基线 ────────────────────────────────────────────────
def _streams(*, respond: bool, n: int = 200, window: int = 10 * MIN):
    rng = random.Random(3)
    her = sorted(NOW - rng.randint(0, 30 * 86_400_000) for _ in range(n))
    his = []
    for t in her:
        if respond:
            his.append(t + rng.randint(1, window))
        else:
            his.append(NOW - rng.randint(0, 30 * 86_400_000))
    return her, sorted(his)


def test_她说话他就回时互惠高() -> None:
    her, his = _streams(respond=True)
    rho = elicitation_ratio(her, his, window_ms=10 * MIN)
    assert rho is not None and rho > 3


def test_彼此独立时互惠接近一() -> None:
    her, his = _streams(respond=False, n=400)
    rho = elicitation_ratio(her, his, window_ms=10 * MIN)
    assert rho is not None and 0.4 < rho < 2.5


def test_样本太少时不给数() -> None:
    her, his = _streams(respond=True, n=5)
    assert elicitation_ratio(her, his) is None
