"""开口决策：一个价值函数 + 一条承诺线。

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
    promise_violated: bool = False       # 声明式承诺线（§14.6），不是节律

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


def evaluate(i: GateInputs) -> GateResult:
    """顺序有意义：承诺线 > 状态 > 通道 > 价值。"""
    if i.promise_violated:
        return GateResult(False, 0.0, "承诺线：这个时段我答应过不打扰")
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
    if v < THRESHOLD:
        return GateResult(False, v, f"价值 {v:.3f} 低于阈值 {THRESHOLD}{tag}")
    return GateResult(True, v, f"价值 {v:.3f} 超过阈值{tag}")


def should_swallow(i: GateInputs) -> bool:
    """吞掉不是失败：它会回流成一条 inner 事件（§5.6）。"""
    return not evaluate(i).allow
