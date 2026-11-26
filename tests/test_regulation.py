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


# ── 正反馈：有界 + 限速 + 限加速度 ───────────────────────────


def test_目标随节奏上升(db) -> None:
    from gits.social.drive import NEUTRAL, target_of

    base = 0.2
    assert target_of(base, base) == pytest.approx(NEUTRAL)
    assert target_of(base * 2, base) > NEUTRAL
    assert target_of(base * 0.5, base) < NEUTRAL
    assert target_of(base * 99, base) <= 1.0, "上界"


def test_乘子在中性时为(db) -> None:
    from gits.social.drive import NEUTRAL, factor

    assert factor(NEUTRAL) == pytest.approx(1.0)
    assert factor(1.0) > 1.0 and factor(0.0) < 1.0
    assert factor(5.0) == factor(1.0), "夹在 0..1"


def test_限速_一小时挪不了多远() -> None:
    from gits.time.regulation import slew
    from gits.social.drive import MAX_RATE

    v, _r = slew(1.0, 0.0, 0.0, 1.0, max_rate=MAX_RATE, max_acc=0.10)
    assert v >= 1.0 - MAX_RATE - 1e-9, f"一小时从 1.0 掉到 {v}"


def test_限加速度_速度不能一步到位() -> None:
    from gits.time.regulation import slew

    v, r = slew(1.0, 0.0, 0.0, 0.1, max_rate=0.15, max_acc=0.10)
    assert abs(r) <= 0.10 * 0.1 + 1e-9, "速度本身也只能一点点变"
    v2, r2 = slew(v, r, 0.0, 0.1, max_rate=0.15, max_acc=0.10)
    assert abs(r2) <= abs(r) + 0.10 * 0.1 + 1e-9, "它在逐渐加速，不是一步跳到最快"


def test_不过冲() -> None:
    from gits.time.regulation import slew

    v, r = slew(0.9, 0.0, 1.0, 100.0, max_rate=0.15, max_acc=0.10)
    assert v == pytest.approx(1.0) and r == 0.0, "到了就停，别绕着目标震"


def test_正反馈方向是对的(db) -> None:
    """说得密 → drive 高 → 同样的证据更想开口。**这是正反馈，不是恒温器。**"""
    from gits.social import drive, state as slow

    for _ in range(10):
        slow.bump(db, "her", "cadence", 0.08, now=T0)
    for h in range(0, 30):
        drive.update(db, "her", now=T0 + h * HOUR)
    high = drive.read(db, "her", now=T0 + 29 * HOUR)

    quiet = DB(str(db.path) + ".2") if hasattr(db, "path") else None
    assert high > 0.5, f"节奏高时 drive 该 > 中性，实际 {high:.2f}"


def test_低方向同样成立(db) -> None:
    from gits.social import drive, state as slow

    for _ in range(6):
        slow.bump(db, "her", "cadence", -0.03, now=T0)
    for h in range(0, 30):
        drive.update(db, "her", now=T0 + h * HOUR)
    assert drive.read(db, "her", now=T0 + 29 * HOUR) < 0.5, \
        "一直说得少，就更难开口——惯性是双向的"


def test_读不写库(db) -> None:
    from gits.social import drive

    drive.update(db, "her", now=T0)
    before = db.scalar("SELECT ts FROM drive WHERE actor='her'")
    for _ in range(3):
        drive.read(db, "her", now=T0 + 5 * HOUR)
    assert db.scalar("SELECT ts FROM drive WHERE actor='her'") == before


def test_压力能对抗向下的惯性(db) -> None:
    """惯性让她越沉默越难开口；压力让她憋久了会开口。**两件事方向相反，都对。**"""
    from gits.speech import gate

    low_drive = gate.GateInputs(unexcluded=0.45, resolvable=0.5, reciprocity=1.0,
                                drive=0.0)
    assert not gate.evaluate(low_drive).allow, "惯性下她不想说"
    assert gate.evaluate(low_drive, pressure=1.0).allow, \
        "但憋得够久，门槛降下来就能说——不是负反馈在压她"


# ── 底气只认外部确认 ────────────────────────────────────────


def test_自己开口不给自己加底气(db) -> None:
    """「我开口了」是我们自己的行为，不是世界给的证据。

    借自心潮·念的 proof 设计：做成事要有认可才算，自己宣布「做好了」不算。
    """
    from gits.social import state as slow

    before = slow.read(db, "her", "confidence", now=T0)
    slow.observe_event(db, "her", "initiate", now=T0)
    after = slow.read(db, "her", "confidence", now=T0)
    assert after == pytest.approx(before), "自己开口不该涨底气"
    assert slow.read(db, "her", "cadence", now=T0) > slow.CFG["cadence"]["baseline"]


def test_他回了才涨底气(db) -> None:
    from gits.social import state as slow

    before = slow.read(db, "her", "confidence", now=T0)
    slow.observe_event(db, "her", "reply_fast", now=T0)
    assert slow.read(db, "her", "confidence", now=T0) > before


def test_能推底气的事件是一张表(db) -> None:
    """散在 if 里的话，下次加事件就会漏掉这条纪律。"""
    from gits.social.state import CONFIDENCE_EVENTS, EVENT_DELTAS

    assert "initiate" not in CONFIDENCE_EVENTS
    assert "reply_fast" in CONFIDENCE_EVENTS
    for name, delta in EVENT_DELTAS.items():
        if "confidence" in delta:
            assert name in CONFIDENCE_EVENTS


def test_沉默的负面仍然小于回应的正面(db) -> None:
    """这条纪律不能因为改上面那条就丢了。"""
    from gits.social.state import EVENT_DELTAS

    fast = EVENT_DELTAS["reply_fast"]["confidence"]
    silent = EVENT_DELTAS["no_reply"]["confidence"]
    assert abs(silent) < fast / 3
