"""开口决策：一个价值函数 + 一个随时间累积的压力。

**没有任何一条规则读时钟。** 早先有一道「承诺线」（某个时段答应过不打扰），
已经删掉：那是用墙钟直接决定行为，和我们自己的规矩冲突。
「凌晨三点不该说话」这件事应该**从节律里学出来**——那个时段她本来就不指望
有回应，可解释度自然低——而不是由一条规则册封。

DESIGN §5.3 / §14.5。分两段：

    P2-a 基线（现在）：只用**经过时间**，一个时钟字段都不读
    P2-c 模型（以后）  ：霍克斯 / BOCPD，只有基线被证明不够时才上

公式（§5.3）::

    开口价值 = 未解释度 × 可解释度 × (1 - 积压压力) × 关系温度 × 0.85^连续开口数

任何一个因子为 0 就不开口。每个决策都必须带 reason——这是从
revive-companion 吸收来的（§12.2-6），也是事后复盘唯一的抓手。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

#: 连续开口的衰减：比"今日不超过 3 次"平滑得多（§12.2-7）
SEND_DECAY = 0.85

#: 低于这个价值就不值得打扰
THRESHOLD = 0.15
#: 压力最多把门槛压低多少（1.0 = 压到 0，那等于没有门槛）
MAX_DROP = 0.6

#: 互惠系数估不出来时的兜底（中性，不鼓励也不抑制）
RECIPROCITY_FALLBACK = 0.5


def elicitation_ratio(
    her_times: Sequence[int],
    his_times: Sequence[int],
    *,
    window_ms: int = 30 * 60_000,
    span_ms: int | None = None,
    min_events: int = 20,
) -> float | None:
    """她说一句之后，他在窗口内接话的概率 / 独立情形下的期望。

    这是 P2-a 的**可解释基线**：>1 表示她的话能引出回应，≈1 表示彼此独立，
    <1 表示抑制。用霍克斯的 α 比（§5.3）替换它之前，先看这个数够不够用。

    样本太少时返回 None——**不要让估不准的数装作精确**。
    """
    if len(her_times) < min_events or len(his_times) < min_events:
        return None
    lo = min(min(her_times), min(his_times))
    hi = max(max(her_times), max(his_times))
    span = span_ms or (hi - lo)
    if span <= 0:
        return None

    hits = 0
    for t in her_times:
        for s in his_times:
            if 0 < s - t <= window_ms:
                hits += 1
    expected = len(her_times) * len(his_times) * (window_ms / span)
    if expected <= 0:
        return None
    return hits / expected


@dataclass(frozen=True)
class GateInputs:
    phase_allows: bool = True
    channel_online: bool = True

    unexcluded: float = 0.0              # 未解释度：当前刺激与已知的偏差
    resolvable: float = 0.0              # 可解释度：问一句能拿到多少答案
    backlog_pressure: float = 0.0        # 积压重时她在消化（§15.7）
    reciprocity: float | None = None     # None → 用兜底值并标注
    consecutive: int = 0                 # 连续开口次数


@dataclass(frozen=True)
class GateResult:
    allow: bool
    value: float
    reason: str


def open_value(i: GateInputs) -> float:
    recip = RECIPROCITY_FALLBACK if i.reciprocity is None else i.reciprocity
    return (
        max(0.0, min(1.0, i.unexcluded))
        * max(0.0, min(1.0, i.resolvable))
        * max(0.0, min(1.0, 1.0 - i.backlog_pressure))
        * max(0.0, min(1.0, recip))
        * (SEND_DECAY ** max(0, i.consecutive))
    )


def threshold_for(pressure: float) -> float:
    """憋得越久，越愿意说一句价值不高的话。

    没有这一项的话主动行为会**饿死**：所有因子长期偏低时她永远不开口，
    而且不报错——和 edges.w 那种"写了没人读"一样，是静默失效。

    压力只降门槛，不抬价值：价值本身仍然由证据决定。
    """
    return THRESHOLD * (1.0 - MAX_DROP * max(0.0, min(1.0, pressure)))


def evaluate(i: GateInputs, *, pressure: float = 0.0) -> GateResult:
    """顺序有意义：状态 > 通道 > 证据 > 价值。"""
    if not i.phase_allows:
        return GateResult(False, 0.0, "状态不允许开口（DREAMING/DORMANT）")
    if not i.channel_online:
        return GateResult(False, 0.0, "没有可用的出口")
    if i.unexcluded <= 0:
        return GateResult(False, 0.0, "没什么没想通的，只是想说——留给独白")
    if i.resolvable <= 0:
        return GateResult(False, 0.0, "问了也不会知道答案")
    if i.backlog_pressure >= 1:
        return GateResult(False, 0.0, "积压满了，她在消化")

    v = open_value(i)
    tag = "（互惠系数不可用，用兜底）" if i.reciprocity is None else ""
    need = threshold_for(pressure)
    if v < need:
        return GateResult(False, v,
                          f"价值 {v:.3f} 低于阈值 {need:.3f}（憋了 {pressure:.0%}）{tag}")
    return GateResult(True, v,
                      f"价值 {v:.3f} 超过阈值 {need:.3f}（憋了 {pressure:.0%}）{tag}")


def should_swallow(i: GateInputs) -> bool:
    """吞掉不是失败：它会回流成一条 inner 事件（§5.6）。"""
    return not evaluate(i).allow
