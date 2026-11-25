"""在岗概率模型：**拟合在 dream，求值在 ghost**。

为什么要分家（实测，见 docs/research/stats-libs.md）：

                      import 耗时        RSS
    numpy             190 ms            ~30 MB
    statsmodels      2119 ms            152 MB     ← ECS 上实测

ghost 常驻、内存上限 ~90 MB，statsmodels 一进来就超了。所以：

    dream  用 statsmodels 拟合（一晚一次，2 秒无所谓）
    系数存进 presence_model 表
    ghost  只做 exp(X·β) 的点积（numpy，微秒级）

这也解释了为什么它比核密度好：泊松 GLM 直接给出**显著性**（p 值）、
能带协变量（周末、节点状态）、能换负二项处理过度离散，
而核密度只能给一条曲线，显著性还得另配一个零分布。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

HOUR = 3_600_000

#: 周期项：(周期小时, 阶数)
#:
#: ⚠️ 24h 取 2 阶时，它的二次谐波**就是** 12h。如果再单独加一个 12h 项，
#: 设计矩阵会秩亏（statsmodels 直接报警告，系数不可辨识）。
#: 所以半日周期不单独写——它已经含在 24h 的二阶里。
DEFAULT_PERIODS: tuple[tuple[float, int], ...] = ((24.0, 2), (168.0, 3))


def _is_weekend(t_ms: int) -> bool:
    """周末标记。固定 +8 偏移，不查 tzdata——它是标签，不是决策。"""
    days = (t_ms // (24 * HOUR)) + 3          # 1970-01-01 是周四
    return (days % 7) in (5, 6)


@dataclass
class PoissonHarmonic:
    """泊松回归 + 傅里叶周期项。系数就是 s(t) 的全部内容。"""

    beta: tuple[float, ...]
    periods: tuple[tuple[float, int], ...] = DEFAULT_PERIODS
    t0_ms: int = 0
    weekend: bool = True
    bin_ms: int = HOUR
    dispersion: float = 1.0     # >1 = 过度离散（成簇的量化指标）
    _max_log: float = field(default=0.0, repr=False)

    # ── 特征 ──
    def features(self, t_ms: int) -> list[float]:
        x = [1.0]
        for period_h, order in self.periods:
            w = 2.0 * math.pi / (period_h * HOUR)
            for m in range(1, order + 1):
                x.append(math.cos(w * m * (t_ms - self.t0_ms)))
                x.append(math.sin(w * m * (t_ms - self.t0_ms)))
        if self.weekend:
            x.append(1.0 if _is_weekend(t_ms) else 0.0)
        return x

    def rate(self, t_ms: int) -> float:
        """每小时的事件率。"""
        z = sum(a * b for a, b in zip(self.features(t_ms), self.beta))
        return math.exp(min(50.0, z))

    def ensure_max(self) -> "PoissonHarmonic":
        week = [sum(a * b for a, b in zip(self.features(self.t0_ms + i * HOUR), self.beta))
                for i in range(168)]
        self._max_log = max(week) if week else 0.0
        return self

    def normalised(self, t_ms: int) -> float:
        if self._max_log <= 0:
            self.ensure_max()
        z = sum(a * b for a, b in zip(self.features(t_ms), self.beta))
        return math.exp(min(50.0, z) - self._max_log)

    # ── 序列化 ──
    def to_json(self) -> str:
        return json.dumps({
            "kind": "poisson_harmonic", "beta": list(self.beta),
            "periods": [list(p) for p in self.periods],
            "t0_ms": self.t0_ms, "weekend": self.weekend,
            "bin_ms": self.bin_ms, "max_log": self._max_log,
            "dispersion": self.dispersion,
        }, ensure_ascii=False)

    @staticmethod
    def from_json(s: str) -> "PoissonHarmonic":
        d = json.loads(s)
        return PoissonHarmonic(
            beta=tuple(float(b) for b in d["beta"]),
            periods=tuple((float(a), int(b)) for a, b in d["periods"]),
            t0_ms=int(d["t0_ms"]), weekend=bool(d["weekend"]),
            bin_ms=int(d["bin_ms"]), _max_log=float(d.get("max_log", 0.0)),
            dispersion=float(d.get("dispersion", 1.0)),
        )


def _binned(events_ms: Sequence[int], bin_ms: int) -> tuple[np.ndarray, int]:
    ts = np.sort(np.asarray(events_ms, dtype=np.int64))
    t0 = int(ts.min())
    nbins = int((int(ts.max()) - t0) // bin_ms) + 1
    counts = np.zeros(nbins)
    np.add.at(counts, ((ts - t0) // bin_ms).astype(int), 1)
    return counts, t0


def _exposure_ms(windows: Sequence[tuple[int, int]] | None, t0: int,
                 nbins: int, bin_ms: int) -> np.ndarray | None:
    """每个桶里**真正被观测到**的毫秒数。

    断线那三天，她根本看不见——那段时间的 0 不是"他不在"，是"没数据"。
    不分青红皂白地把空桶算成零，会把断线学成一个规律的低谷。
    """
    if windows is None:
        return None
    exp = np.zeros(nbins)
    for a, b in windows:
        lo = max(a, t0)
        hi = min(b, t0 + nbins * bin_ms)
        if hi <= lo:
            continue
        i0 = int((lo - t0) // bin_ms)
        i1 = int((hi - t0 - 1) // bin_ms)
        for i in range(i0, i1 + 1):
            s = max(lo, t0 + i * bin_ms)
            e = min(hi, t0 + (i + 1) * bin_ms)
            exp[i] += max(0, e - s)
    return exp


def fit(events_ms: Sequence[int], *,
        observed: Sequence[tuple[int, int]] | None = None,
        periods: tuple[tuple[float, int], ...] = DEFAULT_PERIODS,
        weekend: bool = True, bin_ms: int = HOUR) -> PoissonHarmonic:
    """泊松 GLM 拟合。**只能在 dream 里调用**（会 import statsmodels）。

    `observed` 是可观测窗口列表。给了它就用**曝光量**（offset）建模：
    每个小时的"机会"时长不同，率要按机会归一化，而不是按桶数。
    不给就退化成"首末事件之间全是观测"——**断线会被学成低谷**。
    """
    try:
        import statsmodels.api as sm
    except ImportError as e:                       # pragma: no cover
        raise ImportError(
            "拟合需要 statsmodels（只在 dream 里装）。ghost 请直接读已存的系数。"
        ) from e

    if len(events_ms) < 20:
        raise ValueError("样本不足 20，别拟合——宁可用 KDE 兜底")
    counts, t0 = _binned(events_ms, bin_ms)
    proto = PoissonHarmonic(beta=(), periods=periods, t0_ms=t0,
                            weekend=weekend, bin_ms=bin_ms)
    keep = np.ones(len(counts), dtype=bool)
    offset = None
    exp = _exposure_ms(observed, t0, len(counts), bin_ms)
    if exp is not None:
        keep = exp > 0                      # 完全没观测的桶直接不要
        counts = counts[keep]
        if len(counts) < 20:
            raise ValueError("可观测的桶不足 20，别拟合——用 KDE 兜底")
        offset = np.log(exp[keep] / bin_ms)  # 以"桶"为单位的机会
    X = np.asarray([proto.features(t0 + i * bin_ms)
                    for i in range(len(keep)) if keep[i]])
    # ★ scale="X2" = 拟泊松：用 Pearson X² 估计离散度并放大标准误。
    # 不做这一步，成簇数据会被当成独立事件，p 值假性极小
    # （实测：chaotic 序列 p=8.8e-6，加了拟泊松之后 0.016）。
    res = sm.GLM(counts, X, family=sm.families.Poisson(),
                 offset=None if offset is None else offset).fit(scale="X2")
    return PoissonHarmonic(
        beta=tuple(float(b) for b in res.params), periods=periods,
        t0_ms=t0, weekend=weekend, bin_ms=bin_ms,
        dispersion=float(getattr(res, "scale", 1.0)),
    ).ensure_max()


def period_p_values(events_ms: Sequence[int], *,
                    periods: tuple[tuple[float, int], ...] = DEFAULT_PERIODS,
                    bin_ms: int = HOUR) -> dict[float, float]:
    """每个周期整体的显著性（各阶系数 p 值取最小）。"""
    import statsmodels.api as sm

    counts, t0 = _binned(events_ms, bin_ms)
    proto = PoissonHarmonic(beta=(), periods=periods, t0_ms=t0, bin_ms=bin_ms)
    X = np.asarray([proto.features(t0 + i * bin_ms) for i in range(len(counts))])
    res = sm.GLM(counts, X, family=sm.families.Poisson()).fit(scale="X2")

    out: dict[float, float] = {}
    i = 1
    for period_h, order in periods:
        ps = []
        for _ in range(order):
            ps += [float(res.pvalues[i]), float(res.pvalues[i + 1])]
            i += 2
        out[period_h] = min(ps)
    return out
