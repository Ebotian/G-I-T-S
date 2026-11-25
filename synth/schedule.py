"""作息生成器：给出**带真值**的合成时间序列。

真值是这套东西的全部意义。没有真值，你只能看曲线"像不像"；
有真值，才能回答"她的判断错在哪几个时刻、错了几次"。

五个场景，各自验证一个不同的机制：

    regular      稳定作息            → 核密度应该收敛
    night_owl    晚睡晚起（双峰）    → 均值会错，分布不会
    drift        半月后整体推后 2h   → 应该漂移过去
    chaotic      每天随机            → 周期图应该明确说"没有节律"
    offline_gap  中间断线 3 天        → 断线期间必须是"未观测"，不是"不在"
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
HOUR = 3_600_000
DAY = 86_400_000

#: 固定的起点：2026-01-05 00:00（周一，CST）。合成数据必须可复现。
START = int(datetime(2026, 1, 5, tzinfo=TZ).timestamp() * 1000)

AT_DESK = "at_desk"
AWAY = "away"
ASLEEP = "asleep"
OFFLINE = "offline"     # 她看不见（通道断），不是他不在

SCENARIOS = ("regular", "night_owl", "drift", "chaotic", "offline_gap")


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
            "segments": [{"start": s.start, "end": s.end, "state": s.state}
                         for s in self.segments],
            "messages": [{"ts": t, "text": x} for t, x in self.messages],
            "inner": self.inner_thoughts,
        }


def _h(day_start: int, hour: float) -> int:
    return day_start + int(hour * HOUR)


#: 一天的模板：(起, 止, 状态)，小时可以超过 24——表示跨到第二天。
#: 跨天的部分由 _normalise 裁掉，不需要在模板里手工拆。
def _profile(scenario: str, day_index: int, total_days: int,
             rng: random.Random) -> list[tuple[float, float, str]]:
    weekend = (day_index % 7) in (5, 6)

    if scenario == "night_owl":
        # 晚睡晚起：2 点睡到 11 点，下午和晚上在工位
        return [(0, 2, ASLEEP), (2, 11, ASLEEP), (11, 14, AWAY),
                (14, 22, AT_DESK), (22, 26, AWAY), (26, 35, ASLEEP)]

    shift = 2.0 if (scenario == "drift" and day_index >= total_days // 2) else 0.0

    if weekend:
        plan = [(0, 9.5, ASLEEP), (9.5, 14, AWAY)]
        if rng.random() < 0.4:
            plan.append((14, 17, AT_DESK))
        plan += [(17, 25.5, AWAY), (25.5, 34, ASLEEP)]
        return [(a, b, st) for a, b, st in plan]

    return [
        (0, 7.5, ASLEEP), (7.5, 9.0, AWAY), (9.0, 12.0, AT_DESK),
        (12.0, 13.5, AWAY), (13.5, 18.0, AT_DESK), (18.0, 23.5, AWAY),
        (23.5, 33.0, ASLEEP),
    ] if shift == 0 else [
        (0, 9.5, ASLEEP), (9.5, 11.0, AWAY), (11.0, 14.0, AT_DESK),
        (14.0, 15.5, AWAY), (15.5, 20.0, AT_DESK), (20.0, 25.5, AWAY),
        (25.5, 35.0, ASLEEP),
    ]


def _normalise(segs: list[Segment]) -> list[Segment]:
    """排序后裁掉重叠：后一天的开头让给前一天跨过来的那一段。"""
    out: list[Segment] = []
    cursor = 0
    for s in sorted(segs, key=lambda x: x.start):
        a = max(s.start, cursor)
        if s.end > a:
            out.append(Segment(a, s.end, s.state))
            cursor = s.end
    return out


def generate(name: str, *, days: int = 30, seed: int = 7) -> Scenario:
    if name not in SCENARIOS:
        raise ValueError(f"未知场景 {name!r}，可选：{SCENARIOS}")
    rng = random.Random(seed)
    sc = Scenario(name=name, seed=seed, days=days)

    if name == "chaotic":
        t = START
        while t < START + days * DAY:
            length = rng.choice([2, 3, 5, 8]) * HOUR
            sc.segments.append(Segment(t, min(t + length, START + days * DAY),
                                       rng.choice([AT_DESK, AWAY, ASLEEP])))
            t += length
    else:
        segs: list[Segment] = []
        for d in range(days):
            day_start = START + d * DAY
            for a, b, st in _profile(name, d, days, rng):
                segs.append(Segment(day_start + int(a * HOUR),
                                    day_start + int(b * HOUR), st))
        sc.segments = _normalise(segs)

    # 断线窗口：整段标成 OFFLINE（她看不见），并覆盖原状态
    if name == "offline_gap" and days >= 8:
        gap_start = START + (days // 2) * DAY + 9 * HOUR
        gap_end = gap_start + 3 * DAY
        kept = [s for s in sc.segments if s.end <= gap_start or s.start >= gap_end]
        kept.append(Segment(gap_start, gap_end, OFFLINE))
        sc.segments = sorted(kept, key=lambda s: s.start)

    # 消息：只在醒着的时间说话
    for d in range(days):
        n = rng.randint(2, 7)
        for _ in range(n):
            t = START + d * DAY + rng.randint(0, 24 * 60 - 1) * 60_000
            if sc.state_at(t) in (AT_DESK, AWAY):
                sc.messages.append((t, ""))
    sc.messages.sort()

    # 独白：一周一两次
    for _ in range(max(1, days // 7)):
        t = START + rng.randint(0, max(1, days - 1)) * DAY + rng.randint(0, 23) * HOUR
        sc.inner_thoughts.append(t)

    return sc
