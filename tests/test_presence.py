"""在岗模型：拟合在 dream，求值在 ghost。

这一层验证两件事：
  1. ghost **不会**把 statsmodels 拖进内存（那是 152 MB / 2.1 s，见 stats-libs.md）
  2. 拟合出来的东西确实能分开"在工位"和"睡觉"
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pytest

from gits.time.presence import DEFAULT_PERIODS, PoissonHarmonic, fit, period_p_values
from synth.schedule import generate

REPO = Path(__file__).resolve().parents[1]


def _probe(sc, fn):
    segs = [(s.start, s.end, s.state) for s in sc.segments]

    def state(t):
        for a, b, st in segs:
            if a <= t < b:
                return st
        return "away"

    times = list(range(sc.start, sc.end, 30 * 60_000))
    desk = [fn(t) for t in times if state(t) == "at_desk"]
    sleep = [fn(t) for t in times if state(t) == "asleep"]
    return desk, sleep


def test_ghost_不导入_statsmodels() -> None:
    """ghost 只读系数。一旦有人把拟合逻辑塞进 import 路径，内存预算就崩了。"""
    code = (
        "import sys; sys.path.insert(0, %r); import gits.time.presence; "
        "print('statsmodels' in sys.modules)" % str(REPO)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.stdout.strip() == "False", r.stderr


def test_拟合能分开在工位和睡觉() -> None:
    sc = generate("regular", days=60, seed=7)
    model = fit([t for t, _ in sc.messages])
    desk, sleep = _probe(sc, model.normalised)
    assert statistics.mean(desk) / statistics.mean(sleep) > 5


def test_混乱作息不会被判成有周期() -> None:
    sc = generate("chaotic", days=60, seed=7)
    p = period_p_values([t for t, _ in sc.messages])
    assert p[24.0] > 0.005, f"误报 24h 周期：p={p[24.0]:.2e}"


def test_有规律作息判得出来() -> None:
    sc = generate("regular", days=60, seed=7)
    p = period_p_values([t for t, _ in sc.messages])
    assert p[24.0] < 1e-6


def test_过度离散被量化() -> None:
    """dispersion > 1 就是在说"数据比泊松更成簇"。它同时解释了为什么要用拟泊松。"""
    sc = generate("regular", days=60, seed=7)
    model = fit([t for t, _ in sc.messages])
    assert model.dispersion > 1.5


def test_谐波不重复_不产生秩亏() -> None:
    """24h 的二阶谐波就是 12h。再单独加 12h 项会让设计矩阵秩亏。"""
    assert all(p != 12.0 for p, _ in DEFAULT_PERIODS)
    sc = generate("regular", days=30, seed=3)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fit([t for t, _ in sc.messages])


def test_系数序列化往返一致() -> None:
    sc = generate("regular", days=30, seed=4)
    m = fit([t for t, _ in sc.messages])
    m2 = PoissonHarmonic.from_json(m.to_json())
    t = sc.start + 5 * 86_400_000 + 10 * 3_600_000
    assert m.rate(t) == pytest.approx(m2.rate(t))
    assert m.normalised(t) == pytest.approx(m2.normalised(t))
    assert m.dispersion == m2.dispersion


def test_样本太少拒绝拟合() -> None:
    with pytest.raises(ValueError):
        fit([0, 1000, 2000])
