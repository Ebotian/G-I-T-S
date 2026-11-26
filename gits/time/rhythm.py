"""节律：从事件流里学"她认为你此刻在"。

DESIGN §14 / docs/research/rhythm.md。四层，全部只用 numpy：

    0  Lomb-Scargle 周期图   —— 到底有没有节律？(事件流不均匀，不能用 FFT)
    1  168 格周期核密度      —— s(t)
    2  变化点 (BOCPD)        —— "今天不一样"
    3  未观测掩膜            —— 没数据不等于不在

墙钟只在这里以**标签坐标**的形式出现（周内小时），不参与任何行为判断。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

BINS_PER_WEEK = 168
"""周内小时格。它是坐标，不是时刻（§14.0 标签层）。"""

TZ_OFFSET_HOURS = 8
"""Asia/Shanghai，无夏令时。标签函数用它，决策函数不用。"""


# ── 0. 周期图 ────────────────────────────────────────────────
def lomb_scargle(t_s: np.ndarray, y: np.ndarray, periods_s: np.ndarray) -> np.ndarray:
    """不均匀采样下的周期图。公式来自 Lomb(1976)/Scargle(1982)。"""
    t = np.asarray(t_s, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(t) < 4:
        return np.zeros_like(periods_s, dtype=float)
    var = float(((y - y.mean()) ** 2).mean())
    if var > 1e-12:
        yc, denom = y - y.mean(), var
    else:
        # 事件流是 0/1 序列：全都一样，减均值会得到全零。
        # 这种情况退化成"相位聚集度"（Rayleigh 型），仍然能测周期。
        yc, denom = y, 1.0

    w = 2.0 * np.pi / np.asarray(periods_s, dtype=float)
    wt = w[:, None] * t[None, :]                       # (n_periods, n_events)
    two = 2.0 * wt
    tau = np.arctan2(np.sin(two).sum(axis=1), np.cos(two).sum(axis=1)) / (2.0 * w)
    arg = wt - (w * tau)[:, None]
    c, s = np.cos(arg), np.sin(arg)
    yb = yc[None, :]
    p = 0.5 * (((yb * c).sum(1)) ** 2 / (c * c).sum(1)
               + ((yb * s).sum(1)) ** 2 / (s * s).sum(1))
    return p / denom


@dataclass(frozen=True)
class Periodicity:
    periods_hours: tuple[float, ...]
    power: tuple[float, ...]
    peak_ratio: float          # 峰值 / 中位数
    has_daily: bool
    has_weekly: bool
    verdict: str

    @property
    def best(self) -> tuple[float, float]:
        i = int(np.argmax(self.power))
        return self.periods_hours[i], self.power[i]


def periodicity_report(t_ms: Sequence[int], *, alpha: float = 0.01,
                      n_null: int = 40, seed: int = 12345) -> Periodicity:
    """先问"你到底有没有节律"。没有的话，后面三层都不该用。

    判据不是"比中位数大几倍"（那是拍的），而是**解析的显著性阈值**：
    事件流退化成相位聚集度后，单个频率的功率在原假设下近似 Exp(1)，
    扫过 M 个频率的最大值服从，于是

        z* = ln(M / alpha)

    为什么不用解析阈值 `z* = ln(M/alpha)`：那个式子假设"每个事件是一次独立试验"。
    但真实的对话是**成簇**的——一次会话里隔十几秒连发五条。簇把有效样本数
    压低、把功率的方差抬高，解析阈值会系统性偏低，于是"无规律"的序列也会
    被判成有日周期（实测：chaotic 场景 8 个种子里 4 个误报）。

    经验零分布自动带上这个方差，不需要假装独立。错误方向也不对称：
    判成有节律 → 她在错误的时间开口；判成没节律 → 她多问一句。**宁可保守。**
    """
    if len(t_ms) < 20:
        return Periodicity((), (), 0.0, False, False, "样本太少，不下结论")

    t = (np.asarray(t_ms, dtype=float) - float(np.min(t_ms))) / 1000.0
    # 只搜 2 小时到 14 天。更短的周期对人类活动无意义，而且极易与日周期混叠。
    periods_h = np.geomspace(2.0, 24 * 14, 160)
    per_s = periods_h * 3600.0
    p = lomb_scargle(t, np.ones_like(t), per_s)
    med = float(np.median(p)) or 1e-12
    ratio = float(p.max()) / med

    def near(arr: np.ndarray, hours: float) -> float:
        i = int(np.argmin(np.abs(periods_h - hours)))
        return float(arr[max(0, i - 2): i + 3].max())

    # ── 经验零分布：**间隔重采样**，不是均匀重撒、也不是环形平移 ──
    #
    # 均匀重撒：把成簇抹平，方差被低估 → 误报（实测 chaotic 10 个种子 4 次）。
    # 环形平移：对纯周期序列，整体平移不改变相位差，周期图是相位不变的
    #          → 零分布和观测一样高 → **真正的节律反而检不出来**（踩过）。
    # 间隔自助：从观测到的间隔里**有放回**抽，重建一条时间轴。
    #          它保留重尾与成簇，破坏的是"每天同一时刻"这层结构。
    i24 = int(np.argmin(np.abs(periods_h - 24)))
    i168 = int(np.argmin(np.abs(periods_h - 168)))
    idx = np.unique(np.concatenate([np.arange(max(0, i24 - 2), i24 + 3),
                                    np.arange(max(0, i168 - 2), i168 + 3)]))
    sub_s = per_s[idx]
    pos24 = int(np.where(idx == i24)[0][0])
    pos168 = int(np.where(idx == i168)[0][0])
    span = float(t.max() - t.min()) or 1.0

    gaps = np.diff(t)
    gaps = gaps[gaps > 0] if (gaps > 0).any() else np.array([3600.0])

    rng = np.random.default_rng(seed)
    nul_d, nul_w = [], []
    for _ in range(n_null):
        tn = np.concatenate([[0.0], np.cumsum(rng.choice(gaps, size=len(gaps),
                                                         replace=True))])
        pn = lomb_scargle(tn, np.ones_like(tn), sub_s)
        nul_d.append(float(pn[max(0, pos24 - 2): pos24 + 3].max()))
        nul_w.append(float(pn[max(0, pos168 - 2): pos168 + 3].max()))
    thr_d = float(np.quantile(nul_d, 1 - alpha))
    thr_w = float(np.quantile(nul_w, 1 - alpha))

    has_daily = near(p, 24) > thr_d
    has_weekly = near(p, 168) > thr_w
    verdict = (
        "有日周期" if has_daily else ("只有周周期" if has_weekly else "看不出稳定节律")
    )
    return Periodicity(
        tuple(float(x) for x in periods_h), tuple(float(x) for x in p), ratio,
        has_daily, has_weekly, verdict,
    )


# ── 1. 周期核密度 ────────────────────────────────────────────
def bin_of(t_ms: int) -> int:
    """周内小时格 0..167（周一 00:00 为 0）。坐标，不是时刻。"""
    hours = t_ms // 3600_000 + TZ_OFFSET_HOURS
    # 1970-01-01 是周四；+3 让周一为 0
    return int((hours + 3 * 24) % BINS_PER_WEEK)


class WeeklyKDE:
    """168 格环形核密度，带遗忘。双峰不会被均值抹平（rhythm.md §7）。"""

    def __init__(self, *, sigma_hours: float = 1.5, half_life_days: float = 28.0) -> None:
        self.sigma_hours = sigma_hours
        self.tau_ms = half_life_days * 86400_000 / math.log(2)
        self.w = np.full(BINS_PER_WEEK, 1e-6)

    def _kernel(self) -> np.ndarray:
        d = np.arange(BINS_PER_WEEK, dtype=float)
        d = np.minimum(d, BINS_PER_WEEK - d)           # 环形距离
        k = np.exp(-0.5 * (d / self.sigma_hours) ** 2)
        return k / k.sum()

    def add(self, t_ms: int, now_ms: int, weight: float = 1.0) -> None:
        if t_ms > now_ms:
            return
        age = now_ms - t_ms
        w = weight * math.exp(-age / self.tau_ms)
        # 环形搬运，O(168)/事件，不用卷积
        self.w += w * np.roll(self._kernel(), bin_of(t_ms))

    def fit(self, events: Iterable[tuple[int, float]], now_ms: int) -> "WeeklyKDE":
        self.w = np.full(BINS_PER_WEEK, 1e-6)
        for t_ms, weight in events:
            self.add(t_ms, now_ms, weight)
        return self

    def density(self, t_ms: int) -> float:
        return float(self.w[bin_of(t_ms)])

    def normalised(self) -> np.ndarray:
        return self.w / self.w.sum()

    def peaks(self, *, min_rel: float = 0.5) -> list[int]:
        n = self.normalised()
        out = []
        for i in range(BINS_PER_WEEK):
            l, r = n[(i - 1) % BINS_PER_WEEK], n[(i + 1) % BINS_PER_WEEK]
            if n[i] >= l and n[i] >= r and n[i] > min_rel * n.max():
                out.append(i)
        return out


# ── 2. 变化点 ────────────────────────────────────────────────
def bocpd(xs: Sequence[float], *, hazard: float = 1 / 168, max_run: int = 336,
          sigma2: float = 1.0, mu0: float = 0.0, kappa0: float = 0.1) -> np.ndarray:
    """Bayesian online changepoint detection（Adams & MacKay 2007）。

    观测模型：均值未知、方差已知的正态，共轭更新。返回每步 P(变化点)。
    """
    r = np.array([1.0])          # 游程长度后验
    mu = np.array([mu0])
    kap = np.array([kappa0])
    out = np.zeros(len(xs))

    def logpdf(x: float, m: np.ndarray, k: np.ndarray) -> np.ndarray:
        var = sigma2 * (1.0 + 1.0 / k)
        return -0.5 * (np.log(2 * np.pi * var) + (x - m) ** 2 / var)

    for i, x in enumerate(xs):
        xf = float(x)
        lp = logpdf(xf, mu, kap)
        lp -= lp.max()
        pred = np.exp(lp)
        # 变化点之后没有任何数据，所以 x 服从**先验**预测分布——
        # 而不是现有游程的预测分布。用后者会让 P(cp) 恒等于 hazard。
        lp0 = logpdf(xf, np.array([mu0]), np.array([kappa0]))
        pred0 = float(np.exp(lp0[0] - max(lp0[0], lp.max())))

        cp = hazard * pred0
        growth = (1.0 - hazard) * r * pred
        total = growth.sum() + cp
        out[i] = cp / total if total > 0 else 0.0

        r = np.concatenate([[cp], growth]) / total
        mu = np.concatenate([[mu0], (kap * mu + xf) / (kap + 1.0)])
        kap = np.concatenate([[kappa0], kap + 1.0])
        if len(r) > max_run:
            r, mu, kap = r[:max_run], mu[:max_run], kap[:max_run]
            r = r / r.sum()
    return out


# ── 3. 未观测掩膜 ────────────────────────────────────────────
@dataclass(frozen=True)
class ObsWindow:
    channel: str
    ts_start: int
    ts_end: int
    state: str  # 'up' | 'down' | 'degraded'

    def covers(self, t_ms: int) -> bool:
        return self.ts_start <= t_ms <= self.ts_end


def s_user(t_ms: int, kde: WeeklyKDE, obs: Sequence[ObsWindow]) -> float | None:
    """她此刻认为你在的概率。

    **返回 None 表示"她当时看不见"**——不是 0。把没观测当成不在，
    会让节律模型学到一个假的低谷（DESIGN §14.4）。
    """
    for w in obs:
        if w.covers(t_ms):
            if w.state in ("down", "degraded"):
                return None
            break
    return kde.density(t_ms)
