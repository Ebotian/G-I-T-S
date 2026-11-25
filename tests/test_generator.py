"""验证生成器本身：它得**真的有统计结构**，否则测的是个玩具。

这一层容易被跳过——数据生成器不写测试，等于用不可信的东西验证不可信的东西。
"""

from __future__ import annotations

import json
from collections import Counter

import numpy as np
import pytest

from synth.schedule import AT_DESK, ASLEEP, DAY, START, generate

HOUR = 3_600_000


def _counts_per_day(sc) -> np.ndarray:
    return np.array([sum(1 for t, _ in sc.messages if lo <= t < lo + DAY)
                     for lo in range(START, START + sc.days * DAY, DAY)])


def test_同种子必得同数据() -> None:
    a = generate("regular", days=20, seed=42)
    b = generate("regular", days=20, seed=42)
    assert json.dumps(a.truth(), sort_keys=True) == json.dumps(b.truth(), sort_keys=True)


def test_换种子必须换数据() -> None:
    a = generate("regular", days=20, seed=42)
    b = generate("regular", days=20, seed=43)
    assert [t for t, _ in a.messages] != [t for t, _ in b.messages]


def test_每天的消息量是过度离散的() -> None:
    """泊松的话 var≈mean；真实聊天是成簇的，所以 var 明显大于 mean。"""
    sc = generate("regular", days=90, seed=5)
    c = _counts_per_day(sc)
    assert c.mean() > 3
    assert c.var() / c.mean() > 1.5, f"没有成簇：var/mean={c.var()/c.mean():.2f}"


def test_消息间隔是重尾的() -> None:
    """会话内间隔以分钟计，会话之间以小时计。CV 远大于 1 才叫重尾。"""
    sc = generate("regular", days=60, seed=6)
    ts = np.array([t for t, _ in sc.messages])
    gaps = np.diff(ts) / 1000.0
    assert gaps.std() / gaps.mean() > 1.5
    assert (gaps > 1800).mean() > 0.02          # 至少 2% 的间隔超过半小时


def test_每天的起床时刻有分布() -> None:
    """如果每天 7:45 整起床，核密度就没有任何东西可学。"""
    sc = generate("regular", days=60, seed=8)
    wakes = []
    for d in range(sc.days):
        lo = START + d * DAY
        for a, b, st in [(s.start, s.end, s.state) for s in sc.segments
                         if lo <= s.start < lo + DAY]:
            if st != ASLEEP:
                wakes.append((a - lo) / HOUR)
                break
    assert len(wakes) > 40
    assert np.std(wakes) > 0.3, f"起床时刻几乎是常数：std={np.std(wakes):.2f}h"


def test_在工位时段有抖动() -> None:
    """泡咖啡、开会、上厕所——在岗不是一整块。"""
    sc = generate("regular", days=10, seed=9)
    per_day = Counter((s.start - START) // DAY for s in sc.segments
                      if s.state == AT_DESK)
    assert np.median(list(per_day.values())) >= 4


def test_混乱场景的在场分布接近均匀() -> None:
    """chaotic 不该有"哪几个小时特别活跃"的结构；有的话说明生成器漏了周期。"""
    sc = generate("chaotic", days=60, seed=10)
    per_hour = np.zeros(24)
    for s in sc.segments:
        if s.state != AT_DESK:
            continue
        h0 = ((s.start // HOUR) + 8) % 24
        per_hour[h0] += (s.end - s.start)
    assert per_hour.max() / max(per_hour.mean(), 1) < 3.0, per_hour.round(0)


def test_有规律场景的在场分布明显不均匀() -> None:
    sc = generate("regular", days=60, seed=11)
    per_hour = np.zeros(24)
    for s in sc.segments:
        if s.state != AT_DESK:
            continue
        h0 = ((s.start // HOUR) + 8) % 24
        per_hour[h0] += (s.end - s.start)
    assert per_hour.max() / max(per_hour.mean(), 1) > 3.0


@pytest.mark.parametrize("scenario", ["regular", "night_owl", "drift", "offline_gap"])
def test_真值无缝覆盖(scenario: str) -> None:
    sc = generate(scenario, days=12, seed=3)
    assert sc.segments[0].start == START
    for a, b in zip(sc.segments, sc.segments[1:]):
        assert a.end == b.start, f"{scenario} 真值有缝：{a.end} vs {b.start}"
    assert sc.segments[-1].end >= START + 12 * DAY
