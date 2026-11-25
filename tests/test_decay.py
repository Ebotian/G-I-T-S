"""遗忘模型：公式行为 + 那个"没有区分力"的结论。

先说结论（这份数据上跑出来的）：
    touches、spacing、recency 三者 AUC 都是 0.93 左右，差距在噪声里。
不是模型不好，是**这份生成器里没有"记忆强度"这个隐变量**——
未来的提及只取决于当前的簇状态，所以"按最近排序"就已经接近上限了。
要检验"间隔效应是否成立"，需要真实数据（见 docs/research/memory-data.md）。
"""

from __future__ import annotations

import pytest

from gits.decay import (
    BASELINES, MODELS, S0, State, build, retrievability,
)
from synth.eval_decay import evaluate
from synth.facts import generate

DAY = 86_400_000
T0 = 1_800_000_000_000


# ── 幂律遗忘 ────────────────────────────────────────────────
def test_刚想起来时_r_等于一() -> None:
    assert retrievability(T0, T0, S0) == pytest.approx(1.0)


def test_越久越模糊且单调() -> None:
    vals = [retrievability(T0 + d * DAY, T0, S0) for d in (0, 1, 3, 7, 30, 365)]
    assert all(a > b for a, b in zip(vals, vals[1:]))
    assert all(0 < v <= 1 for v in vals)


def test_稳定性越大忘得越慢() -> None:
    t = T0 + 10 * DAY
    assert retrievability(t, T0, 10 * DAY) > retrievability(t, T0, 1 * DAY)


def test_幂律不是指数() -> None:
    """指数衰减在半衰期之后会掉到 0.25，幂律要慢得多——这是 FSRS 的核心主张。"""
    r = retrievability(T0 + 4 * DAY, T0, 2 * DAY)
    assert r > 0.4


# ── 三个模型的行为差异 ──────────────────────────────────────
def test_次数模型__每提一次_S_都涨() -> None:
    st = build("touches", T0, [])
    prev = st.s
    for i in range(1, 5):
        st.recall(T0 + i * DAY, "touches")
        assert st.s > prev
        prev = st.s


def test_间隔模型__快忘了再想起增益更大() -> None:
    """这是 B 与 A 的全部差别，也是"间隔效应"的可检验形式。"""
    fresh = build("spacing", T0, [])
    fresh.recall(T0 + int(0.1 * DAY), "spacing")     # R 还很高时想起
    gain_fresh = fresh.s / S0

    stale = build("spacing", T0, [])
    stale.recall(T0 + 10 * DAY, "spacing")           # R 已经很低时想起
    gain_stale = stale.s / S0

    assert gain_stale > gain_fresh * 1.5


def test_次数模型_增益与时机无关() -> None:
    """A 的问题：不管多模糊，涨的幅度一样。"""
    a = build("touches", T0, [])
    a.recall(T0 + 1 * DAY, "touches")
    b = build("touches", T0, [])
    b.recall(T0 + 20 * DAY, "touches")
    assert a.s == pytest.approx(b.s)


def test_actr__近期多次提及得分更高() -> None:
    recent = build("actr", T0, [T0 + 90 * DAY, T0 + 92 * DAY, T0 + 95 * DAY])
    old = build("actr", T0, [T0 + 1 * DAY, T0 + 3 * DAY, T0 + 6 * DAY])
    assert recent.score(T0 + 100 * DAY) > old.score(T0 + 100 * DAY)


def test_基线与候选都在表里() -> None:
    assert set(BASELINES) <= set(MODELS)
    assert {"touches", "spacing", "actr"} <= set(MODELS)


# ── 端到端：这个评测本身得有信号 ────────────────────────────
def test_评测跑得通且优于随机() -> None:
    fs = generate(days=120, seed=7)
    s = evaluate(fs, "touches")
    assert s.n_eval > 5
    assert 0.5 < s.auc <= 1.0
    assert 0.0 <= s.p10 <= 1.0


def test_频率基线明显弱于时间感知模型() -> None:
    """如果连这条都测不出来，说明评测没有区分力。"""
    fs = generate(days=120, seed=7)
    assert evaluate(fs, "touches").auc > evaluate(fs, "frequency").auc + 0.05


@pytest.mark.xfail(
    reason="这份合成数据里没有'记忆强度'这个隐变量，三个候选打不出差异——"
           "这是结论，不是 bug。见 docs/research/memory-data.md")
def test_间隔效应在合成数据上优于次数模型() -> None:
    fs = generate(days=180, seed=7)
    assert evaluate(fs, "spacing").auc > evaluate(fs, "touches").auc + 0.02
