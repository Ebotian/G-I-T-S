"""用合成数据验证机制——这是这套东西存在的全部理由。

每个断言都在问同一个问题：**她的判断和真值差多少？**
不是"跑不跑得通"，而是"错在哪、错了几次"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gits.db import DB, Writer
from gits.events import export_logset, import_logset
from gits.project import dump_projection, restore
from gits.rhythm import WeeklyKDE, bin_of
from synth.build import build
from synth.schedule import AT_DESK, DAY, SCENARIOS, START
from synth.score import score

HOUR = 3_600_000


def _b(tmp: Path, scenario: str, days: int = 30) -> tuple[Path, Path]:
    b = build(tmp / scenario, scenario=scenario, days=days, seed=7)
    return b.db_path, b.truth_path


# ── 场景 → 判定 ─────────────────────────────────────────────
@pytest.mark.parametrize("scenario", ["regular", "night_owl", "drift", "offline_gap"])
def test_有规律的场景应该被学出来(tmp_path: Path, scenario: str) -> None:
    db, truth = _b(tmp_path, scenario)
    r = score(db, truth)
    assert r.daily, f"{scenario} 没检出日周期（ratio={r.peak_ratio:.1f}）"
    assert r.separation > 5, f"{scenario} 分不开在工位和睡觉（{r.separation:.2f}）"
    assert r.false_speak == 0


def test_混乱作息不该被假装知道(tmp_path: Path) -> None:
    """没有节律时必须说"没有"。硬套一个时段表，只会让她在错误的时间开口。"""
    db, truth = _b(tmp_path, "chaotic")
    r = score(db, truth)
    assert not r.daily
    assert r.separation < 3
    # 这条是反转的：模型没有信息，误开口率必然大于 0。
    # 指标的意义在于**它能看出失败**，而不只是能看出成功。
    assert r.false_speak > 0.02


def test_断线期间是未观测而不是不在(tmp_path: Path) -> None:
    db, truth = _b(tmp_path, "offline_gap")
    r = score(db, truth)
    assert r.unseen_ok, "断线期间 s_user 不该给出数值"
    assert r.backfill_trusted == 0, "补传数据不该被当成可信发生时间"


def test_作息漂移会跟着走(tmp_path: Path) -> None:
    db, truth = _b(tmp_path, "drift", days=30)
    with DB(db) as d:
        ts = [r["ts"] for r in d.q(
            "SELECT ts FROM events WHERE kind IN ('acoustic','msg_in')"
            " AND ts_trust=1 ORDER BY ts")]
    kde = WeeklyKDE(half_life_days=28).fit([(t, 1.0) for t in ts], max(ts))
    ref = ts[0]
    old = kde.density(ref + (10 - bin_of(ref)) * HOUR)    # 前半月的上午
    new = kde.density(ref + (16 - bin_of(ref)) * HOUR)    # 后半月推后之后
    assert new > old * 1.4, f"没有漂移过去：old={old:.1f} new={new:.1f}"


# ── 真值本身要可信 ──────────────────────────────────────────
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_真值覆盖完整且不重叠(tmp_path: Path, scenario: str) -> None:
    _, truth_path = _b(tmp_path, scenario, days=10)
    segs = [(s["start"], s["end"], s["state"]) for s in
            json.loads(truth_path.read_text(encoding="utf-8"))["segments"]]
    assert segs[0][0] == START
    for (a1, b1, _), (a2, b2, _) in zip(segs, segs[1:]):
        assert b1 == a2, f"真值有缝或重叠：{b1} vs {a2}"
    assert segs[-1][1] >= START + 10 * DAY - DAY


# ── 合成数据也要能重放 ─────────────────────────────────────
def test_合成数据上重放可复现(tmp_path: Path) -> None:
    db_path, _ = _b(tmp_path, "regular", days=5)
    with DB(db_path) as db:
        restore(db)
        first = dump_projection(db).canonical()
        payload = json.loads(json.dumps(export_logset(db), default=bytes.decode))
        restore(db)
        assert dump_projection(db).canonical() == first

    with DB(tmp_path / "replica.db") as rep:
        import_logset(rep, payload)
        assert restore(rep) > 0
        assert dump_projection(rep).canonical() == first


def test_重复灌入不会产生重复事件(tmp_path: Path) -> None:
    db_path, _ = _b(tmp_path, "regular", days=3)
    with DB(db_path) as db:
        n0 = db.scalar("SELECT count(*) FROM events")
        payload = json.loads(json.dumps(export_logset(db), default=bytes.decode))
        import_logset(db, payload)
        assert db.scalar("SELECT count(*) FROM events") == n0


def test_批量写入也走幂等键(tmp_path: Path) -> None:
    with DB(tmp_path / "g.db") as db:
        w = Writer(db)
        row = {"ts": START, "source": "wechat", "kind": "msg_in", "ext_id": "x1"}
        assert w.append_events_bulk([row, row]) == 1
        assert w.append_events_bulk([row]) == 0


# ── 规模：用本地资源压一压 ─────────────────────────────────
@pytest.mark.slow
def test_一年规模的写入与重放(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    db_path, _ = _b(tmp_path, "regular", days=365)
    build_s = time.perf_counter() - t0

    with DB(db_path) as db:
        n = db.scalar("SELECT count(*) FROM events")
        t1 = time.perf_counter()
        restore(db)
        replay_s = time.perf_counter() - t1
        size_mb = Path(db_path).stat().st_size / 1e6

    assert n > 10_000
    assert build_s < 60, f"生成太慢：{build_s:.1f}s"
    assert replay_s < 30, f"重放太慢：{replay_s:.1f}s"
    print(f"\n一年合成数据：{n} 条事件 · {size_mb:.1f} MB · "
          f"生成 {build_s:.1f}s · 重放 {replay_s:.2f}s")
