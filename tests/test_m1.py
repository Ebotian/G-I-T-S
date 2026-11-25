"""M1：上下文装配的缓存纪律 + 节律在合成数据上的行为。

不联网、不调模型。所有时间序列都是合成的，但结构是真的：
  - 日周期 + 抖动 → 应该被检出
  - 均匀随机      → 不应该被误报为有节律
  - 双峰作息      → 核密度应该保住两个峰，均值会丢掉它们
  - 通道断线      → s_user 必须是 None，不能是 0
"""

from __future__ import annotations

import hashlib
import random
import re

import numpy as np
import pytest

from gits.context import (
    PersonaSeed, RelationSnapshot, TurnContext, assemble, bucket,
    estimate_tokens, render_a, render_b, render_c, status_line,
)
from gits.rhythm import (
    BINS_PER_WEEK, ObsWindow, WeeklyKDE, bin_of, bocpd, periodicity_report,
    s_user,
)

NOW = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000


def ts_bin(b: int, days_ago: int = 0) -> int:
    """取一个落在周内第 b 格、且距离 NOW 整数天的时刻（整数天不改变格子）。"""
    return NOW + (b - bin_of(NOW)) * HOUR - days_ago * DAY


# ── 上下文装配 ────────────────────────────────────────────────
SEED = PersonaSeed(narrative="我不太会哄人。但我记得住你说过的话。", version=1)


def _snap(b: int = 0) -> RelationSnapshot:
    return RelationSnapshot(bucket=b, summary="认识 47 天，最近在聊 Rust 和你的睡眠。",
                            hubs=("Rust 所有权", "他熬夜"), phase="醒着")


def _ctx(text: str = "在吗") -> TurnContext:
    return TurnContext(stimulus=text, recalled=("上次他说 CRDT 太麻烦",),
                       recent=(("她", "你今天几点睡的"), ("他", "三点")))


def test_a段逐字稳定_与时间无关() -> None:
    """A 段在同一版叙事下，无论过多久、B/C 怎么变，都必须逐字相同。"""
    p1 = assemble(SEED, _snap(0), _ctx("在吗"))
    p2 = assemble(SEED, _snap(999_999), _ctx("完全不同的一句话 " + "x" * 500))
    assert p1.a == p2.a
    assert p1.a_hash == p2.a_hash


def test_a段里没有时刻() -> None:
    """一旦 A 段混进时间，整条 prompt 后面的缓存全废（50 倍价差）。"""
    a = render_a(SEED)
    assert not re.search(r"\d{1,2}:\d{2}", a)
    assert not re.search(r"20\d{2}-\d{2}-\d{2}", a)


def test_a段随叙事版本改变() -> None:
    v2 = PersonaSeed(narrative="我改主意了。", version=2)
    assert render_a(SEED) != render_a(v2)


def test_缓存布局_前缀必须成立() -> None:
    p = assemble(SEED, _snap(), _ctx())
    assert p.text.startswith(p.a)
    assert p.text.startswith(p.a + p.b)
    assert p.text.endswith(p.c)


def test_b段按小时快照() -> None:
    assert bucket(NOW) == bucket(NOW + 30 * 60_000)      # 同一小时内不变
    assert bucket(NOW) != bucket(NOW + 61 * 60_000)      # 跨小时才变


def test_预算在声明范围内() -> None:
    p = assemble(SEED, _snap(), _ctx())
    b = p.budget()
    assert 20 <= b["a"] <= 400
    assert b["b"] <= 300
    assert b["c"] <= 400
    # estimate_tokens 每段各 +1，所以合计允许 ±3 的偏差
    assert abs(b["total"] - (b["a"] + b["b"] + b["c"])) <= 3


def test_estimate_tokens_单调() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 150) > estimate_tokens("a" * 15)


def test_status_行里没有墙钟() -> None:
    line = status_line(backlog=3, s_user=None, reciprocity=None, quota_left=12, stale=True)
    assert "s_user=?" in line and "fallback" in line and "只读" in line
    assert not re.search(r"\d{1,2}:\d{2}", line)


# ── 节律：先问有没有 ─────────────────────────────────────────
def _daily_events(days: int = 30, hour_bin: int = 9, jitter_s: int = 1800) -> list[int]:
    rng = random.Random(7)
    return [ts_bin(hour_bin, d) + rng.randint(-jitter_s, jitter_s) * 1000
            for d in range(1, days + 1)]


def test_检出日周期() -> None:
    rep = periodicity_report(_daily_events())
    assert rep.has_daily, rep.verdict
    assert rep.peak_ratio > 5


def test_随机事件不应被误报为有节律() -> None:
    rng = random.Random(11)
    ts = sorted(rng.randint(NOW - 30 * DAY, NOW) for _ in range(300))
    rep = periodicity_report(ts)
    assert not rep.has_daily, f"误报：{rep.verdict} ratio={rep.peak_ratio:.1f}"


# ── 节律：核密度 ────────────────────────────────────────────
def test_双峰作息不会被均值抹平() -> None:
    kde = WeeklyKDE()
    for d in range(1, 29):
        kde.add(ts_bin(6, d), NOW)     # 早睡型
        kde.add(ts_bin(23, d), NOW)    # 晚睡型
    early, late, middle = kde.density(ts_bin(6)), kde.density(ts_bin(23)), kde.density(ts_bin(14))
    assert early > middle and late > middle
    assert len(kde.peaks()) >= 2


def test_密度会随新作息漂移() -> None:
    """半年前的作息应该被忘掉，最近的不该。"""
    kde = WeeklyKDE(half_life_days=14)
    for d in range(200, 231):
        kde.add(ts_bin(9, d), NOW)          # 半年前是早上 9 点
    for d in range(1, 31):
        kde.add(ts_bin(20, d), NOW)         # 现在改成了晚上 8 点
    # 注意别用相近的格子测：sigma=1.5h 的核会把相邻 2 小时糊在一起，
    # 那是核密度的正常行为，不是遗忘失效。
    assert kde.density(ts_bin(20)) > kde.density(ts_bin(9)) * 20


def test_格子是环形的() -> None:
    assert 0 <= bin_of(NOW) < BINS_PER_WEEK
    assert bin_of(NOW + BINS_PER_WEEK * HOUR) == bin_of(NOW)


# ── 节律：变化点 ────────────────────────────────────────────
def test_变化点能被找到() -> None:
    rng = np.random.default_rng(3)
    xs = np.concatenate([rng.normal(0, 1, 200), rng.normal(5, 1, 100)])
    p = bocpd(xs, sigma2=1.0)
    assert 195 <= int(np.argmax(p)) <= 215, int(np.argmax(p))
    assert p.max() > 0.1


def test_平稳序列不该有变化点() -> None:
    rng = np.random.default_rng(5)
    p = bocpd(rng.normal(0, 1, 300), sigma2=1.0)
    assert p.max() < 0.1


# ── 节律：未观测 ≠ 沉默 ─────────────────────────────────────
def test_通道断线时_s_user_是_None_而不是_0() -> None:
    kde = WeeklyKDE()
    for d in range(1, 29):
        kde.add(ts_bin(10, d), NOW)
    obs = [ObsWindow("wechat", NOW - 3 * HOUR, NOW, "down")]
    assert s_user(NOW - HOUR, kde, obs) is None
    assert s_user(NOW - 2 * DAY, kde, []) is not None


def test_未观测时段不参与密度估计() -> None:
    """断线期间的"空白"不能被当成"他不在"灌进核密度。"""
    kde = WeeklyKDE()
    for d in range(1, 15):
        kde.add(ts_bin(10, d), NOW)
    before = kde.density(ts_bin(20))
    kde.add(ts_bin(20, 1), NOW, weight=0.0)   # 未观测样本权重为 0
    assert kde.density(ts_bin(20)) == pytest.approx(before)


def test_密度权重随时间衰减() -> None:
    kde = WeeklyKDE(half_life_days=7)
    kde.add(ts_bin(10, 0), NOW)
    fresh = kde.density(ts_bin(10))
    kde2 = WeeklyKDE(half_life_days=7)
    kde2.add(ts_bin(10, 70), NOW)
    assert fresh > kde2.density(ts_bin(10)) * 10
