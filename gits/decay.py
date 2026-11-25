"""遗忘机制：三个候选，一个接口。

**要预测的不是"哪条是答案"，而是"哪条还会被再提起"。**

我一开始把评测写成"30 天后问其中 30 件，看能不能选对"，那是错的：
如果答案定义成"最近被提起的那条"，那么"按最近排序"就 100% 正确，
三个模型完全一样。**真正要检验的是遗忘本身**——在没有任何新信号的时候，
哪条记忆该留在手边。所以任务是**预测未来会不会被提起**（这就是 FSRS
那套参数拟合的目标函数，只不过它的标签是"复习时是否回忆成功"）。

三个模型：

    A  touches   每被提起一次 S 涨一点，跟当时多模糊无关
    B  spacing   S 的增益取决于召回时的可提取性 R：R 越低增益越大（间隔效应）
    C  actr      base-level activation：B = ln Σ (t − t_j)^−d
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from gits.fsrs import Memory, Rating

DAY = 86_400_000

#: 幂律遗忘的两个常数（FSRS 用的就是 19/81 与 −0.5）
FACTOR = 19 / 81
DECAY = -0.5

#: 初始稳定性（按"多久会忘一半"的量级取：2 天）
S0 = 2 * DAY


def retrievability(t: int, last: int, s: float) -> float:
    """r(t)：幂律遗忘。t 与 last 都是毫秒时间戳。"""
    dt = max(0, t - last)
    return (1 + FACTOR * dt / max(s, 1.0)) ** DECAY


@dataclass
class State:
    """一条记忆的动力学状态。由提及时间轴推出来，不需要额外存储。"""

    born: int
    touches: int = 0
    last: int = 0
    s: float = S0
    recalls: list[int] = field(default_factory=list)

    @classmethod
    def from_mentions(cls, born: int, times: list[int], model: str) -> "State":
        st = cls(born=born, last=born, s=S0)
        for t in sorted(times):
            st.recall(t, model)
        return st

    def recall(self, t: int, model: str) -> None:
        r = retrievability(t, self.last, self.s)
        self.touches += 1
        self.recalls.append(t)
        if model == "touches":
            # A：次数决定稳定性，跟当时的模糊程度无关
            self.s = S0 * (1 + 0.3 * math.log(1 + self.touches))
        elif model == "spacing":
            # B：间隔效应——快要忘的时候想起来，增益最大
            self.s = self.s * (1 + 2.0 * (1.0 - r))
        # C 不用 S：直接看提及时间轴的激活
        self.last = t

    def score(self, t: int) -> float:
        raise NotImplementedError


@dataclass
class Touches(State):
    """A：只有次数。"""

    def score(self, t: int) -> float:
        return retrievability(t, self.last, self.s)


@dataclass
class Spacing(Touches):
    """B：次数 + 间隔效应。"""


@dataclass
class ActR(State):
    """C：base-level activation，d≈0.5，不维护 S。"""

    d: float = 0.5

    def score(self, t: int) -> float:
        if not self.recalls:
            return -math.inf
        total = sum(max(1.0, t - tj) ** -self.d for tj in self.recalls if tj <= t)
        return math.log(total) if total > 0 else -math.inf


@dataclass
class Recency(State):
    """基线：只看"上一次是什么时候"。"""

    def score(self, t: int) -> float:
        return -float(t - self.last)


@dataclass
class Frequency(State):
    """基线：只看"提过几次"。"""

    def score(self, t: int) -> float:
        return float(self.touches)


@dataclass
class Fsrs(State):
    """FSRS-5（默认权重）。**先验地采用社区长期实践**——我们还没有自己的数据。

    与 A/B/C 的区别不只是公式：它有难度 D 的均值回归、有 lapse 的重算，
    而且 S 的增益同时受 D、S、R 三者影响。
    """

    mem: object = field(default_factory=lambda: Memory())

    def recall(self, t: int, model: str) -> None:
        self.mem.review(t, Rating.GOOD)      # 我们只有"用上了"这一个正向信号
        self.touches += 1
        self.recalls.append(t)
        self.last = t
        self.s = self.mem.s * DAY

    def score(self, t: int) -> float:
        return self.mem.r(t)


MODELS = {"fsrs": Fsrs, "touches": Touches, "spacing": Spacing, "actr": ActR,
          "recency": Recency, "frequency": Frequency}

#: 前三个是候选方案，后两个是基线——没有基线就说不清"多出来的复杂度买到了什么"
BASELINES = ("recency", "frequency")


def build(model: str, born: int, times: list[int]) -> State:
    cls = MODELS[model]
    st = cls(born=born, last=born, s=S0)
    for t in sorted(times):
        st.recall(t, model)
    return st
