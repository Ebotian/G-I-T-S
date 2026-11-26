"""调节量：均值的回归、慢变量、低频基线。

这个模块存在的理由：**同一个公式我们写过三遍**。

    attention.reestimate   base ← base + λ(target − base)
    graph.decay            w · e^(−Δt/τ)
    affect.advance         1 − e^(−Δt/τ)

三种写法、三个速率常数、三套注释——它们是同一个东西：

    dX = θ(μ − X)dt          （Ornstein–Uhlenbeck 的确定性部分）

离散形式就是 `X′ = X + θ·(μ − X)·dt`，等价于 `μ + (X − μ)·e^(−θ·dt)`。
两种写法我们以前都用过，这就是它们看起来不像同一件事的原因。

## 关于「慢的做快的基线」——能不能写成傅里叶级数

能，而且我们已经有两个现成的：

    presence.PoissonHarmonic  逐日/逐周谐波（sin/cos 回归）——**就是傅里叶级数**
    rhythm.WeeklyKDE          168 格环形密度——周期的、低频的

严格说，「慢的做快的基线」是**时频分解**：

    低频（周期、稳定）  →  基线 trend/seasonality
    高频（事件、瞬时）  →  在基线附近的偏离

这正是结构性时间序列（structural time series）的写法，
而在我们的数据量下，**不需要真的做 FFT**：

    基线 = 长 τ 的指数加权（`low_pass`）——它天然只剩下低频分量
    快变量 = 观测值 − 基线

谐波拟合是它的升级路径：等某一维攒够几百个点、且**确有周期性**时，
把 `low_pass` 换成 `PoissonHarmonic` 那样的两三项谐波即可，接口不用变。
现在换是拿三个点去拟合一条正弦曲线——那不是建模，是许愿。
"""

from __future__ import annotations

import math

DAY_H = 24.0


def mean_revert(value: float, baseline: float, theta: float, dt_hours: float) -> float:
    """离散 OU 的确定性部分。**唯一允许的均值回归写法。**

        X′ = μ + (X − μ)·e^(−θ·dt)

    θ 的单位是 1/小时：θ=0.05 表示时间常数 20 小时。
    dt=0 时原样返回；θ→∞ 时直接跳到基线。
    """
    if dt_hours <= 0:
        return float(value)
    if theta <= 0:
        return float(value)
    return float(baseline) + (float(value) - float(baseline)) * math.exp(-theta * dt_hours)


def relax(value: float, baseline: float, tau_hours: float, dt_hours: float) -> float:
    """同上，但用时间常数表达（τ 比 θ 好读：τ=20h 就是 20 小时走掉 63%）。"""
    if tau_hours <= 0:
        return float(baseline)
    return mean_revert(value, baseline, 1.0 / tau_hours, dt_hours)


def low_pass(prev: float, observed: float, tau_hours: float,
             dt_hours: float) -> float:
    """低通：只留下慢分量。这是「慢的做快的基线」最省事的那一版。

        base′ = base + (obs − base)·(1 − e^(−dt/τ))

    τ 越大越"迟钝"。用几百个点做谐波回归当然更准，但那要有几百个点。
    """
    if dt_hours <= 0:
        return float(prev)
    k = 1.0 - math.exp(-dt_hours / max(1e-6, tau_hours))
    return float(prev) + (float(observed) - float(prev)) * k


def slew(prev: float, rate: float, target: float, dt_hours: float, *,
         max_rate: float, max_acc: float) -> tuple[float, float]:
    """二阶限幅：**限速 + 限加速度，且不过冲**。

    为什么需要二阶：只限速率的话，速率本身可以一步跳到最大，
    于是曲线在起点最陡、后面平掉——看起来像「突然想要，然后慢慢泄气」。
    限加速度之后它只能逐渐加速，形状才是连续的。

    返回 (新值, 新速率)。
    """
    if dt_hours <= 0:
        return float(prev), float(rate)
    err = float(target) - float(prev)
    want = max(-max_rate, min(max_rate, err / dt_hours))
    lo, hi = rate - max_acc * dt_hours, rate + max_acc * dt_hours
    new_rate = max(lo, min(hi, want))
    new = prev + new_rate * dt_hours
    # 不过冲：越过目标、或者正好落在目标上，都停在目标并让速度归零。
    # （只判"越过"不够：正好命中时速度还留着，下一拍会继续推着走。）
    if (target - new) * (target - prev) <= 0:
        return float(target), 0.0
    return new, new_rate
