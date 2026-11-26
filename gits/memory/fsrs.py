"""FSRS-5：先验地采用社区长期实践的公式与默认权重。

为什么用它的默认值：**我们还没有自己的数据**。在"没有证据"和"有大量
他人在几亿次复习上拟合出来的参数"之间，先验地选后者（DESIGN §5.1.2 的
"先不上 19 参数"说的是**不要自己拟合**，不是"不要用它"）。

用到的是 19 个权重里的 15 个（w0–w14），剩下的属于同日子复习与短时记忆，
我们不做排程，用不上。

评分映射（社区用 Again/Hard/Good/Easy，我们只有隐式信号）：

    她确实用上了这条记忆           → Good(3)
    用上了但他纠正了 / 与事实矛盾   → Again(1)
    同一轮里被反复调用             → Easy(4)
    —— 没有 Hard。凭空的"有点难"没有信息量。

时间单位：内部用**天**（FSRS 的原生单位），对外接口收发毫秒。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DAY = 86_400_000

#: FSRS-5 默认权重（w0..w18）。见 VERIFIED §6。
W = (0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.0234,
     1.616, 0.1544, 1.0824, 1.9813, 0.0953, 0.2975, 2.2042, 0.2407,
     2.9466, 0.5034, 0.6567)

FACTOR = 19 / 81
DECAY = -0.5

D_MIN, D_MAX = 1.0, 10.0


class Rating:
    AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


def clamp_d(d: float) -> float:
    return min(D_MAX, max(D_MIN, d))


def retrievability(elapsed_days: float, s: float) -> float:
    """R(t,S) = (1 + 19/81 · t/S)^(−0.5)"""
    if s <= 0:
        return 0.0
    return (1 + FACTOR * max(elapsed_days, 0.0) / s) ** DECAY


def initial_difficulty(g: int) -> float:
    """D0(G) = w4 − e^{w5·(G−1)} + 1"""
    return clamp_d(W[4] - math.exp(W[5] * (g - 1)) + 1.0)


def initial_stability(g: int) -> float:
    """S0(G) = w[G−1]；单位天。Good 的初值是 3.13 天。"""
    return max(W[g - 1], 0.1)


def next_difficulty(d: float, g: int) -> float:
    """D' = D − w6·(G−3)，再做均值回归。"""
    d2 = d - W[6] * (g - 3)
    d3 = W[7] * initial_difficulty(4) + (1 - W[7]) * d2
    return clamp_d(d3)


def next_stability(s: float, d: float, r: float, g: int) -> float:
    """成功时用 w8–w10 的间隔效应式；失败时用 w11–w14 的 lapse 式。"""
    if g == Rating.AGAIN:
        # 忘掉了：稳定性重算，且不允许比之前更大
        s_new = (W[11] * (d ** -W[12]) * ((s + 1) ** W[13] - 1)
                 * math.exp(W[14] * (1 - r)))
        return max(0.1, min(s_new, s))
    # 想起来了：R 越低（越接近遗忘）增益越大——这就是间隔效应
    inc = math.exp(W[8]) * (11 - d) * (s ** -W[9]) * (math.exp(W[10] * (1 - r)) - 1)
    return max(0.1, s * (1 + inc))


@dataclass
class Memory:
    """一条记忆的 FSRS 状态。单位都是天（时间戳另存）。"""

    s: float = 0.0
    d: float = 0.0
    last_ms: int | None = None
    reviews: int = 0

    def review(self, t_ms: int, g: int) -> None:
        if self.last_ms is None or self.s <= 0:
            self.s = initial_stability(g)
            self.d = initial_difficulty(g)
        else:
            elapsed = max(0.0, (t_ms - self.last_ms) / DAY)
            r = retrievability(elapsed, self.s)
            self.s = next_stability(self.s, self.d, r, g)
            self.d = next_difficulty(self.d, g)
        self.last_ms = t_ms
        self.reviews += 1

    def r(self, t_ms: int) -> float:
        if self.last_ms is None or self.s <= 0:
            return 0.0
        return retrievability(max(0.0, (t_ms - self.last_ms) / DAY), self.s)


def implicit_rating(*, used: bool, corrected: bool = False,
                    repeated_in_turn: bool = False) -> int:
    """把隐式信号翻成评分。**没有 Hard**：凭空的"有点难"没有信息量。"""
    if corrected:
        return Rating.AGAIN
    if repeated_in_turn:
        return Rating.EASY
    return Rating.GOOD if used else Rating.GOOD


def build(times_ms: list[int], *, corrections: set[int] | None = None) -> Memory:
    """按时间轴重放一串"被用到"的记录。`corrections` 是索引集合（那些次被纠正了）。"""
    m = Memory()
    corrections = corrections or set()
    for i, t in enumerate(sorted(times_ms)):
        m.review(t, Rating.AGAIN if i in corrections else Rating.GOOD)
    return m


# ── 情绪接进记忆：两个不同的东西，别混 ────────────────────────
#
#   事件自身的情感强度  →  影响**编码**（这条记忆存得多牢）
#   她此刻的心情        →  影响**检索**（现在想起什么）
#
# 合在一起会形成回路：难过 → 难过的事记得更牢 → 更容易想起难过的事 → 更难过。
# 这种回路跑几个月才显形，而那时候已经很难拆。

#: 事件情感强度对稳定性的增益上限。AROUSAL_GAIN = 0 时完全退化成纯 FSRS。
AROUSAL_GAIN = 0.5
#: 心情一致性对检索打分的增益上限（只影响排序，不删候选）
MOOD_GAIN = 0.3
#: 同一天最多巩固几次。之后不再加深——一个下午说三十遍，前六遍就够了。
#: （这不是稳定性的绝对上限，是**当天增量的次数上限**。）
SHORT_TERM_STEPS = 6


def short_term_factor() -> float:
    """FSRS-5 的短期记忆项：S ′ = S · e^(w17·(g−3+w18))。

    我们没有评分，就取 g=3（中性），于是它退化成常数：
    e^(0.5034 × 0.6567) ≈ 1.39。**参数是社区拟合的，不是我们拍的。**
    """
    return math.exp(W[17] * (0.0 + W[18]))


def same_day_boost(n: int) -> float:
    """同一天第 n 次提到时的巩固倍数。

    **递减**：第一次最有用，后面越来越小。心理学的说法是 session 内收益递减；
    朴素的说法是——一个下午说三十遍同一件事，不会让它记得三十倍牢。

        n=1  → ×1.39
        n=2  → ×1.20
        n=5  → ×1.08

    没有这个的话，同日重复对稳定性**贡献为零**：
    漏斗的复现数正确地不算它们（那是独立重述），
    但「它确实被重复了一次」这件事也不该完全消失。
    """
    if n <= 0:
        return 1.0
    return 1.0 + (short_term_factor() - 1.0) / n


def arousal_boost(intensity: int | None, *, gain: float = AROUSAL_GAIN) -> float:
    """事件自身的情感强度 → 编码增益。强度 1/2/3 映射到 0/0.5/1。

    这是心理学里最硬的一类结论（情绪唤醒增强巩固）。
    **gain = 0 时精确等于纯 FSRS**——这是它唯一的验证方式。
    """
    if not intensity or gain <= 0:
        return 1.0
    ar = (max(1, min(3, int(intensity))) - 1) / 2.0
    return 1.0 + gain * ar


def mood_congruence(angle: str | None, intensity: int | None,
                    cells: dict[tuple[str, int], float]) -> float:
    """这条记忆的情感标签和**她此刻的分布**有多一致（0..1）。

    用圆盘几何：角度差换算成余弦相似，强度差按比例。
    对面（joy vs sadness）应当是 0，不是 1。
    """
    if not angle or not cells:
        return 0.0
    angles = _angles()
    total = 0.0
    for (a, i), p in cells.items():
        if p <= 0:
            continue
        far = _angle_distance(str(angle), a, angles)
        level = abs(int(intensity) - int(i)) / 2.0
        # 同一个角度同一个强度 → 1；对面、强度相差两档 → 0
        total += p * max(0.0, 1.0 - 0.5 * far - 0.5 * level)
    return min(1.0, total)


def _angles() -> tuple[str, ...]:
    from gits.social.affect import ANGLES

    return ANGLES


def _angle_distance(a: str, b: str, angles: tuple[str, ...]) -> float:
    """圆盘上的最短角距，归一化到 0..1（0=同一个角，1=对面）。"""
    if a not in angles or b not in angles:
        return 1.0
    i, j = angles.index(a), angles.index(b)
    n = len(angles)
    d = abs(i - j) % n
    return (min(d, n - d)) / (n / 2)


# ── 检索即强化：spacing effect 的落地 ────────────────────────
#
# 在这之前，`s` **只在创建时被写一次**。一条记忆被想起来十次和一次，
# 稳定性完全一样——于是「反复间隔重复的东西越来越牢」这句话在代码里是假的。
#
# 强化的时机放在**真的被用进上下文之后**，不是 recall() 里：
# 读路径不该写库（它在热路径上，而且「召回了但没用」不该算一次练习）。

#: 没有难度记录时用中值（FSRS 的 D 在 1..10）
DEFAULT_D = 5.0
#: 单次强化的上限倍数。FSRS 的公式是为**主动复习**设计的——
#: 在 r=0.2 时它会给出 36 倍。被动想起不该等价于一次刻意的复习。
REINFORCE_MAX_GAIN = 3.0


def reinforce(s: float, d: float | None, r: float) -> tuple[float, float]:
    """被想起来一次之后的新 (s, d)。

    评分固定取 **g=3（Good）**：它被用上了，这就够了。
    不去猜「她费了多大劲」——那是我们无法观测的东西。

    间隔效应由公式本身负责：注意 `(e^(w10·(1−r)) − 1)` 这一项，
    **r 越低，涨得越多**。间隔越久还想起得来，巩固就越强。
    
    顺带说明：w15/w16（hard penalty / easy bonus）仍然没用——
    它们要真正的评分，而我们还没有。宁缺毋滥。
    """
    d0 = DEFAULT_D if d is None else max(1.0, min(10.0, float(d)))
    r0 = max(0.0, min(1.0, float(r)))
    s0 = max(0.1, float(s))
    s_new = next_stability(s0, d0, r0, 3)
    # 被动想起 ≠ 主动复习，涨得太猛就打住（见 REINFORCE_MAX_GAIN 的说明）
    s_new = min(s_new, s0 * REINFORCE_MAX_GAIN)
    d_new = next_difficulty(d0, 3)
    return max(0.1, s_new), d_new
