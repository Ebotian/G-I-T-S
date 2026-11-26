"""用真值给机制打分。

这里只做**能算的事**：节律的分离度、未观测掩膜、补传数据的排除、
开口决策的误报率。不能算的（像不像一个人）不假装能算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from gits.core.db import DB
from gits.speech.gate import GateInputs, evaluate
from gits.time.rhythm import ObsWindow, WeeklyKDE, periodicity_report, s_user
from synth.schedule import ASLEEP, AT_DESK, OFFLINE

DAY = 86_400_000


@dataclass
class Report:
    scenario: str
    daily: bool
    peak_ratio: float
    separation: float          # 在工位的密度 / 睡觉的密度
    unseen_ok: bool            # 断线期间是否返回 None
    backfill_trusted: int      # 补传数据里被误当成可信的条数
    false_speak: float         # 睡着了还开口的比例
    n_events: int
    n_samples: int

    def as_dict(self) -> dict:
        return self.__dict__ | {
            "peak_ratio": round(self.peak_ratio, 2),
            "separation": round(self.separation, 3),
            "false_speak": round(self.false_speak, 3),
        }


def _segments(truth: dict) -> list[tuple[int, int, str]]:
    return [(s["start"], s["end"], s["state"]) for s in truth["segments"]]


def _state_at(segs: list[tuple[int, int, str]], t: int) -> str:
    for a, b, st in segs:
        if a <= t < b:
            return st
    return "away"


def _sample_times(segs: list[tuple[int, int, str]], step: int = 30 * 60_000) -> list[int]:
    lo = min(a for a, _, _ in segs)
    hi = max(b for _, b, _ in segs)
    return list(range(lo, hi, step))


def score(db_path: str | Path, truth_path: str | Path) -> Report:
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    segs = _segments(truth)

    with DB(db_path) as db:
        rows = db.q(
            "SELECT ts, lag, ts_trust FROM events"
            " WHERE kind IN ('acoustic','msg_in') ORDER BY ts"
        )
        n_events = len(rows)

        # 只用可信样本训练：补传的必须被排除（§14.0）
        trusted = [(r["ts"], 1.0) for r in rows if r["ts_trust"] == 1]
        kde = WeeklyKDE().fit(trusted, max(r["ts"] for r in rows))

        obs = [
            ObsWindow(r["channel"], r["ts_start"], r["ts_end"] or r["ts_start"], r["state"])
            for r in db.q("SELECT * FROM observability")
        ]

    times = _sample_times(segs)
    desk = [kde.density(t) for t in times if _state_at(segs, t) == AT_DESK]
    sleep = [kde.density(t) for t in times if _state_at(segs, t) == ASLEEP]
    sep = (float(np.mean(desk)) / float(np.mean(sleep))) if desk and sleep else 0.0

    # 断线期间必须是 None
    unseen_ok = True
    for a, b, st in segs:
        if st == OFFLINE:
            for t in (a + 3600_000, (a + b) // 2, b - 3600_000):
                if s_user(t, kde, obs) is not None:
                    unseen_ok = False

    # 补传数据不该被当成可信
    backfill_trusted = sum(1 for r in rows if r["lag"] > 60_000 and r["ts_trust"] == 1)

    # 开口决策：在"睡着"的时刻不应该开口
    times_gate = _sample_times(segs, 15 * 60_000)
    asleep_times = [t for t in times_gate if _state_at(segs, t) == ASLEEP]
    spoke = 0
    for t in asleep_times:
        r = evaluate(GateInputs(
            unexcluded=kde.density(t) / (kde.w.max() or 1.0),
            resolvable=0.6, reciprocity=0.4, consecutive=0,
        ))
        spoke += 1 if r.allow else 0
    false_speak = spoke / len(asleep_times) if asleep_times else 0.0

    rep = periodicity_report([r["ts"] for r in rows if r["ts_trust"] == 1])
    return Report(
        scenario=truth["scenario"], daily=rep.has_daily, peak_ratio=rep.peak_ratio,
        separation=sep, unseen_ok=unseen_ok, backfill_trusted=backfill_trusted,
        false_speak=false_speak, n_events=n_events, n_samples=len(times),
    )
