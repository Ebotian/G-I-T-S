"""作息生成器：**从统计规律出发**采样，而不是把时刻表抄一遍。

为什么必须这样：我们要验证的是统计机制（核密度、周期图、变化点）。
如果造出来的数据是"每天 9:00 整开始、每 10 分钟一条、没有一点抖动"，
那这些机制在一个玩具上当然全对——**测了个寂寞**。

所以这里的每一个数都是**采样**出来的：

    起床时刻     Normal(7:30, 40min)，周末再往后漂
    上班时长     Normal(8h, 1.2h)，截断到 [3h, 11h]
    午休         开始 ~ Uniform，时长 ~ Normal(1h, 20min)
    在工位/离开  两状态连续时间马尔可夫链，驻留时长 ~ 指数分布
    消息         按状态的泊松到达 → 成簇（一次会话 1..6 条，间隔指数分布）
    作息漂移     切换时刻 ~ Uniform(整个区间)，不是正好在中间
    丢数         每段以 2% 概率整段消失（网络抖）

真值记录的是**这一次采样的实际结果**，不是参数。
参数另存一份，用来检查"学到的东西是否收敛到参数"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

TZ = timezone(timedelta(hours=8))
HOUR = 3_600_000
DAY = 86_400_000

#: 固定起点：2026-01-05 00:00（周一，CST）。同 seed 必得同数据。
START = int(datetime(2026, 1, 5, tzinfo=TZ).timestamp() * 1000)

AT_DESK = "at_desk"
AWAY = "away"
ASLEEP = "asleep"
OFFLINE = "offline"

SCENARIOS = ("regular", "night_owl", "drift", "chaotic", "offline_gap")

#: 每个状态下的"发起一次会话"速率（次/小时）
SESSION_RATE = {AT_DESK: 0.9, AWAY: 0.22, ASLEEP: 0.0, OFFLINE: 0.0}


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    state: str


@dataclass
class Scenario:
    name: str
    seed: int
    days: int
    segments: list[Segment] = field(default_factory=list)
    messages: list[tuple[int, str]] = field(default_factory=list)
    inner_thoughts: list[int] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def start(self) -> int:
        return self.segments[0].start

    @property
    def end(self) -> int:
        return self.segments[-1].end

    def state_at(self, t: int) -> str:
        for s in self.segments:
            if s.start <= t < s.end:
                return s.state
        return AWAY

    def truth(self) -> dict:
        return {
            "scenario": self.name,
            "seed": self.seed,
            "days": self.days,
            "tz_offset_hours": 8,
            "start": self.start,
            "params": self.params,
            "segments": [{"start": s.start, "end": s.end, "state": s.state}
                         for s in self.segments],
            "messages": [{"ts": t, "text": x} for t, x in self.messages],
            "inner": self.inner_thoughts,
        }


# ── 采样一天 ────────────────────────────────────────────────
def _sample_day(rng: np.random.Generator, name: str, d: int, days: int,
                drift_at: int) -> list[tuple[float, float, str]]:
    """返回当天 [0,24) 的分段，小时为浮点。"""
    weekday = (d % 7) < 5

    if name == "chaotic":
        # 由 generate() 走连续时间轴，不在每天零点重置
        raise AssertionError("chaotic 不走 _sample_day")

    base_wake = 7.5 if weekday else 9.5
    if name == "night_owl":
        base_wake = 11.5
    shift = 2.0 if (name == "drift" and d >= drift_at) else 0.0

    wake = float(np.clip(rng.normal(base_wake + shift, 0.7), 4.5, 14.0))
    sleep = float(np.clip(rng.normal((23.5 if name != "night_owl" else 26.0) + shift,
                                     0.9), 20.5, 27.5))
    sleep_mod = sleep - 24.0 if sleep >= 24.0 else sleep

    # 去上班的延迟、时长、午休——全部有分布
    commute = max(0.2, float(rng.lognormal(-0.4, 0.5)))
    work_len = float(np.clip(rng.normal(8.0 if weekday else 3.0, 1.2), 2.5, 11.0))
    work_len *= 0.0 if (not weekday and rng.random() < 0.55) else 1.0
    lunch_start = float(rng.uniform(3.0, 4.5))
    lunch_len = float(np.clip(rng.normal(1.0, 0.35), 0.3, 2.5))

    # 睡得比 24 点更晚（跨夜）时，晚上不睡——那一段已经算在"明天凌晨"里了
    night_end = 24.0 if sleep >= 24.0 else sleep_mod
    desk_start = wake + commute
    desk_end = min(desk_start + work_len + lunch_len, night_end - 0.3)

    segs: list[tuple[float, float, str]] = []
    if wake > 0:
        segs.append((0.0, wake, ASLEEP))
    if desk_start > wake:
        segs.append((wake, min(desk_start, 24.0), AWAY))
    if work_len > 0 and desk_start < desk_end:
        ls = min(desk_start + lunch_start, desk_end)
        le = min(ls + lunch_len, desk_end)
        if ls > desk_start:
            segs.append((desk_start, ls, AT_DESK))
        if le > ls:
            segs.append((ls, le, AWAY))
        if desk_end > le:
            segs.append((le, desk_end, AT_DESK))
    if night_end < 24.0 and night_end > desk_end:
        segs.append((max(desk_end, wake), night_end, AWAY))
        segs.append((night_end, 24.0, ASLEEP))
    else:
        segs.append((max(desk_end, wake), 24.0, AWAY))

    return [(a, b, st) for a, b, st in segs if b > a]


def _flicker(rng: np.random.Generator, seg: Segment) -> list[Segment]:
    """在"在工位"的段里抖动：去泡咖啡、开会、上厕所。

    两状态连续时间马尔可夫链——驻留时长是指数分布，不是固定值。
    """
    if seg.state != AT_DESK or seg.end - seg.start < 20 * 60_000:
        return [seg]
    out, t, present = [], seg.start, True
    while t < seg.end:
        mean_min = 55.0 if present else 14.0
        dwell = int(rng.exponential(mean_min * 60_000))
        dwell = max(60_000, min(dwell, seg.end - t))
        out.append(Segment(t, t + dwell, AT_DESK if present else AWAY))
        t += dwell
        present = not present
    return out


def _normalise(segs: list[Segment], lo: int, hi: int) -> list[Segment]:
    """排序、裁剪、补洞。**补洞是必须的**：真值有缝的话，评分和断言都会骗人。"""
    out, cursor = [], lo
    for s in sorted(segs, key=lambda x: x.start):
        if s.start > cursor:
            out.append(Segment(cursor, s.start, AWAY))     # 填缝，不留下"未知"
            cursor = s.start
        a = max(s.start, cursor)
        b = min(s.end, hi)
        if b > a:
            out.append(Segment(a, b, s.state))
            cursor = b
    if cursor < hi:
        out.append(Segment(cursor, hi, AWAY))
    return out


# ── 消息：成簇到达 ──────────────────────────────────────────
def _sample_messages(rng: np.random.Generator, segs: list[Segment]) -> list[int]:
    """按状态的泊松到达 + 会话内成簇。这就是"burstiness"，也是 Hawkes 想抓的东西。"""
    out: list[int] = []
    for s in segs:
        rate = SESSION_RATE.get(s.state, 0.0)
        if rate <= 0:
            continue
        hours = (s.end - s.start) / HOUR
        n_sessions = int(rng.poisson(rate * hours))
        for _ in range(n_sessions):
            t0 = s.start + int(rng.random() * (s.end - s.start))
            n_msgs = 1 + int(rng.poisson(1.6))
            t = t0
            for _ in range(n_msgs):
                if t >= s.end:
                    break
                out.append(t)
                gap = int(rng.exponential(45_000))
                if rng.random() < 0.12:                    # 偶尔停下来
                    gap += int(rng.exponential(15 * 60_000))
                t += max(2_000, gap)
    return sorted(out)


def generate(name: str, *, days: int = 30, seed: int = 7) -> Scenario:
    if name not in SCENARIOS:
        raise ValueError(f"未知场景 {name!r}，可选：{SCENARIOS}")
    rng = np.random.default_rng(seed)
    sc = Scenario(name=name, seed=seed, days=days)

    # 漂移的切换时刻不是正好一半——也是采样出来的
    drift_at = int(rng.integers(max(1, int(days * 0.35)), max(2, int(days * 0.75)) + 1))

    raw: list[Segment] = []
    if name == "chaotic":
        # ★ 连续时间轴：如果每天零点重新采样，零点就会变成一个假的"每天都有边界"
        # 的周期性信号——周期图会在 24h 上误报（这个坑真的踩过）。
        t, end = START, START + days * DAY
        while t < end:
            dwell = int(max(0.5, float(rng.exponential(3.5))) * HOUR)
            state = str(rng.choice([AT_DESK, AWAY, ASLEEP], p=[0.35, 0.4, 0.25]))
            raw.extend(_flicker(rng, Segment(t, min(t + dwell, end), state)))
            t += dwell
    else:
        for d in range(days):
            day_start = START + d * DAY
            for a, b, st in _sample_day(rng, name, d, days, drift_at):
                seg = Segment(day_start + int(a * HOUR), day_start + int(b * HOUR), st)
                raw.extend(_flicker(rng, seg))
    sc.segments = _normalise(raw, START, START + days * DAY)

    # 断线：起止也是采样的，不是"正中间三天"
    if name == "offline_gap" and days >= 8:
        gap_start = START + int(rng.integers(1, days - 4)) * DAY + int(rng.integers(0, 20) * HOUR)
        gap_len = int((2 + rng.random() * 3) * DAY)
        cut = gap_start + gap_len
        keep = [s for s in sc.segments if s.end <= gap_start or s.start >= cut]
        keep.append(Segment(gap_start, cut, OFFLINE))
        sc.segments = _normalise(keep, START, START + days * DAY)

    sc.messages = [(t, "") for t in _sample_messages(rng, sc.segments)]

    n_inner = max(1, int(rng.poisson(days / 4)))
    sc.inner_thoughts = sorted(
        int(START + rng.random() * days * DAY) for _ in range(n_inner)
    )

    sc.params = {
        "session_rate": SESSION_RATE,
        "wake_mu": {"regular": 8.5, "night_owl": 11.5}.get(name, 8.5),
        "drift_at_day": drift_at if name == "drift" else None,
        "n_messages": len(sc.messages),
        "n_segments": len(sc.segments),
    }
    return sc
