"""慢变量 / 共鸣 / 底气 / 句式。

每一组都在盯一个具体的错法：

  惯性丢了       频繁交流后被冷落，立刻掉回陌生人的距离
  沉默当拒绝     no_reply 的负面幅度被写大 → 她越说越不敢说
  文风硬编码     对谁、什么心情，说的话一模一样
  共鸣无限放大   一句像旧事的话把价值顶到 1.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech import gate, style, unresolved
from gits.social import state as slow

T0 = 1_800_000_000_000
HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


# ── 均值回归只有一种写法 ────────────────────────────────────


def test_时间常数和速率是同一个东西() -> None:
    from gits.time.regulation import mean_revert, relax

    assert mean_revert(1.0, 0.0, 1 / 20, 20) == pytest.approx(
        relax(1.0, 0.0, 20, 20))
    assert relax(1.0, 0.0, 20, 20) == pytest.approx(0.3679, abs=1e-3)


def test_三处衰减给出同一个答案(db) -> None:
    """graph / affect / attention 以前各写一套 exp，现在必须同源。"""
    from gits.memory.graph import decay
    from gits.social.affect import decay as adecay
    from gits.time.regulation import relax

    assert decay(T0 + 24 * HOUR, T0, tau_days=1.0) == pytest.approx(
        relax(1.0, 0.0, 24.0, 24.0))
    assert adecay(24 * HOUR, tau_hours=24.0) == pytest.approx(
        relax(1.0, 0.0, 24.0, 24.0))


# ── 惯性 ─────────────────────────────────────────────────────


def test_频繁之后被冷落不会立刻掉回基线(db) -> None:
    for _ in range(5):
        slow.bump(db, "her", "cadence", 0.06, now=T0)
    just = slow.read(db, "her", "cadence", now=T0)
    half_day = slow.read(db, "her", "cadence", now=T0 + 12 * HOUR)
    three_days = slow.read(db, "her", "cadence", now=T0 + 3 * DAY)
    base = slow.CFG["cadence"]["baseline"]
    assert just > half_day > three_days >= base
    assert half_day > base * 1.8, "半天就掉回陌生人距离，那不叫惯性"


def test_低频也会保持低频(db) -> None:
    """"一直很少说话"也是一种惯性，不该隔天就"恢复热情"。"""
    for _ in range(3):
        slow.bump(db, "her", "cadence", -0.04, now=T0)
    low = slow.read(db, "her", "cadence", now=T0)
    later = slow.read(db, "her", "cadence", now=T0 + 2 * HOUR)
    assert low < slow.CFG["cadence"]["baseline"]
    assert later < slow.CFG["cadence"]["baseline"], "两小时后还该是低频"


def test_沉默的负面远小于回应的正面(db) -> None:
    fast = slow.EVENT_DELTAS["reply_fast"]["confidence"]
    none_ = slow.EVENT_DELTAS["no_reply"]["confidence"]
    assert fast > 0 > none_
    assert abs(none_) < fast / 3, "沉默是含糊的，不能当成拒绝"


def test_事件驱动而不是文本驱动(db) -> None:
    """涨落只来自可观测行为；没有任何一个事件是模型判断出来的。"""
    for name in slow.EVENT_DELTAS:
        assert "sad" not in name and "angry" not in name
    got = slow.observe_event(db, "her", "reply_fast", now=T0)
    assert got["cadence"] > slow.CFG["cadence"]["baseline"]


def test_慢变量有界(db) -> None:
    for _ in range(50):
        slow.bump(db, "her", "confidence", +0.5, now=T0)
    assert slow.read(db, "her", "confidence", now=T0) <= 1.0


# ── 共鸣 ─────────────────────────────────────────────────────


def test_共鸣抬高价值但有界(db) -> None:
    b = dict(unexcluded=0.8, resolvable=0.9, reciprocity=1.0)
    base = gate.open_value(gate.GateInputs(**b))
    full = gate.open_value(gate.GateInputs(**b, resonance=1.0))
    assert full > base
    assert full <= base * (1 + gate.RESONANCE_GAIN) + 1e-9
    assert gate.open_value(gate.GateInputs(**b, resonance=99)) == pytest.approx(full)


def test_没有旧记忆时没有共鸣(db) -> None:
    _i, _p, _b = unresolved.inputs(db, now=T0)
    assert _i.resonance == 0.0


# ── 底气 ─────────────────────────────────────────────────────


def test_底气低的时候同样证据也不够开口(db) -> None:
    b = dict(unexcluded=0.5, resolvable=0.6, reciprocity=1.0)
    low = gate.open_value(gate.GateInputs(**b, confidence=0.1))
    mid = gate.open_value(gate.GateInputs(**b, confidence=0.5))
    high = gate.open_value(gate.GateInputs(**b, confidence=1.0))
    assert low < mid < high


def test_底气缺省不参与(db) -> None:
    b = dict(unexcluded=0.5, resolvable=0.6, reciprocity=1.0)
    assert gate.open_value(gate.GateInputs(**b)) == pytest.approx(
        gate.open_value(gate.GateInputs(**b, confidence=0.5)))


def test_输入里会带上底气和共鸣(db) -> None:
    slow.bump(db, "her", "confidence", +0.2, now=T0)
    i, _p, _b = unresolved.inputs(db, now=T0)
    assert i.confidence is not None and i.confidence > 0.5


# ── 文风不硬编码 ─────────────────────────────────────────────


def test_节奏密的时候要求更短(db) -> None:
    for _ in range(12):
        slow.bump(db, "her", "cadence", 0.08, now=T0)
    line = style.line(db, "her", now=T0)
    assert "短" in line


def test_底气低的时候不许把话说满(db) -> None:
    slow.bump(db, "her", "confidence", -0.35, now=T0)
    line = style.line(db, "her", now=T0)
    assert "可能" in line or "底气" in line


def test_没什么可调的时候不占位置(db) -> None:
    assert style.line(db, "her", now=T0) == "", "默认样子不该多出一行"


def test_文风只在_B_段不在_A_段(db) -> None:
    """A 段一天一变；塞进会变的东西等于每轮把缓存前缀打碎。"""
    from gits.speech.context import assemble, PersonaSeed, RelationSnapshot, TurnContext

    seed = PersonaSeed(narrative="我不太会哄人。", version=1)
    snap = RelationSnapshot(bucket=0, summary="认识第 3 天。", hubs=(),
                            phase="醒着", style="说得短一点。")
    p = assemble(seed, snap, TurnContext(stimulus="在吗"))
    assert "说得短一点" not in p.a
    assert "说得短一点" in p.b
