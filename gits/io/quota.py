"""配额：每个月固定的预算，怎么分到"每个个体、每一天"。

DESIGN §18.6。三条口径：

  1. **月度上限固定** → 每天每人是派生出来的，不是另外拍的
  2. **配额给她用，不是给她花**：她"想做的事"通常比额度长，所以会用掉大部分；
     但她没有义务用光——**为烧额度而行动是失败，不是达标**
  3. **内容是她的**：系统只记元数据（做了什么类型的活动、花了多少、产出几个），
     不记她读了什么、写了什么。那属于 self 域，不进仪表盘

价格来自 VERIFIED §1-2；夜间是空闲时段，半价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ── 单价（元）。全部来自 VERIFIED，改这里之前先去核对价格页 ──
PRICE = {
    "llm_in_hit": 0.02 / 1e6,     # 每 token，空闲时段
    "llm_in": 1.0 / 1e6,
    "llm_out": 4.0 / 1e6,
    "fetch": 5.0 / 1000,
    "search": 5.0 / 1000,
    "ocr_page": 1.5 / 1000,
    "tts_char": 3.0 / 10000,
    "asr_hour": 0.78,
    "exec_sec": 0.0,              # 本地算力，电费不算在她头上
}

#: 额度类型。前六类花的是钱，最后一个是本地时间片。
KINDS = ("llm_in", "llm_out", "fetch", "search", "tts_char", "asr_hour", "exec_sec")

#: 换算用的单价（元 / 单位）。llm_in 用"半命中"的折中价——宁可保守。
UNIT_PRICE = {
    "llm_in": 0.5 * PRICE["llm_in"] + 0.5 * PRICE["llm_in_hit"],
    "llm_out": PRICE["llm_out"],
    "fetch": PRICE["fetch"],
    "search": PRICE["search"],
    "tts_char": PRICE["tts_char"],
    "asr_hour": PRICE["asr_hour"],
    "exec_sec": 0.0,
}


@dataclass(frozen=True)
class Budget:
    """月度总预算。这是唯一的固定量，其余全部派生。"""

    monthly_cny: float = 200.0
    days_per_month: int = 30
    #: 对话主线（只有主个体有通道）+ 系统开销
    conversation_share: float = 0.70
    #: 自主活动：不在你面前时她做的事
    autonomy_share: float = 0.30
    #: "她"的实例数（§13.4：三个平等的实例）
    actors: int = 3
    #: 自主活动里，模型 token 与抓取/工具各占多少
    token_share: float = 0.75
    tool_share: float = 0.25
    #: 本地算力配额（不受钱约束，只受机器约束）
    exec_minutes_per_day: int = 20

    #: ── 结转口径：周清日结转 ──
    #: 周内没花完的会留到后面几天（做得了"连续三天读一个东西"这种项目），
    #: 但一周结束清零（否则会一路攒成"大事基金"）。
    week_days: int = 7
    #: 单日上限 = 日额度 × 这个倍数。**防止一天把攒下的花光**——
    #: 结转提供了自由度，也必须配一个速率上限，否则"攒额上限"就等于"当日额度"。
    burst_factor: float = 3.0
    #: 攒额上限 = 日额度 × 这个天数
    accrue_cap_days: float = 5.0


@dataclass(frozen=True)
class Allowance:
    """一个个体一天的额度。钱 + 次数 + 时间，三种单位分开给。"""

    actor: str
    cny: float
    units: dict[str, float] = field(default_factory=dict)

    def pretty(self) -> str:
        """**这是上限，不是目标。** 打印出来是为了让"额度有多少"可感知，
        不是为了让谁去凑满它。"""
        u = self.units
        return (f"{self.actor}: {self.cny:.3f} 元/天（上限）· "
                f"≈{u.get('fetch', 0):.0f} 次抓取 · "
                f"≈{u.get('llm_out', 0) / 1000:.0f}k 输出 token · "
                f"{u.get('exec_sec', 0) / 60:.1f} 分钟本地算力")


def day_cap(b: Budget, a: "Allowance") -> float:
    """单日能花的上限（含结转）。"""
    return a.cny * b.burst_factor


def autonomy_pool(b: Budget) -> float:
    """每天分给"自主活动"的总钱数。"""
    return b.monthly_cny * b.autonomy_share / b.days_per_month


def allowance(b: Budget, actor: str) -> Allowance:
    """每个个体的每日额度。**三等分**——§13.4 的平等是这么落到钱上的。"""
    daily = autonomy_pool(b) / max(1, b.actors)
    token_cny = daily * b.token_share
    tool_cny = daily * b.tool_share
    units = {
        "llm_out": token_cny / UNIT_PRICE["llm_out"],
        "llm_in": token_cny / UNIT_PRICE["llm_in"],
        "fetch": tool_cny / UNIT_PRICE["fetch"],
        "search": tool_cny / UNIT_PRICE["search"] * 0.5,
        # 本地算力不花钱，按时间三等分
        "exec_sec": b.exec_minutes_per_day * 60 / max(1, b.actors),
    }
    return Allowance(actor=actor, cny=daily, units=units)


def allowances(b: Budget, actors: Iterable[str]) -> dict[str, Allowance]:
    names = list(actors)
    if len(names) != b.actors:
        raise ValueError(f"预算按 {b.actors} 个实例分，但给了 {len(names)} 个")
    return {n: allowance(b, n) for n in names}


@dataclass
class Account:
    """额度账户。像电子钱包一样：每天入账，花的时候扣余额。

    规则（DESIGN §18.6）：

        每日入账  daily            每天自动 +daily，所以不会"花完就卡到周一"
        攒额上限  daily × accrue_cap_days   攒不下太多，防止变成"大事基金"
        日消费上限 daily × 3      防止一天把余额花光
        周末清零  每周日 24:00 余额归零（周一从 +daily 重新开始）
        不花不罚  没有任何"不用就扣"的机制

    "余额 + 日上限" 和上一版的"周锅"是同一件事的两种写法；这一版的优点是
    **入账是显式的**——她能看见"今天又到了 0.667 元"，而不是一个只在周初
    存在的池子。钱每天多一点，比钱每周少一点更像过日子。
    """

    actor: str
    balance: float = 0.0
    spent_today: float = 0.0
    spent_week: float = 0.0
    entries: list[tuple[str, float]] = field(default_factory=list)

    # ── 入账 ──
    def accrue(self, daily: float, cap: float) -> float:
        """新的一天开始：入账一次，且不许攒超过上限。返回实际入账额。"""
        before = self.balance
        self.balance = min(self.balance + daily, cap)
        self.spent_today = 0.0
        got = round(self.balance - before, 9)
        self.entries.append(("入账", got))
        return got

    # ── 额度 ──
    def day_cap(self, daily: float, factor: float = 3.0) -> float:
        return daily * factor

    def spendable(self, daily: float, factor: float = 3.0) -> float:
        """今天还能花多少 = min(余额, 日上限 − 今天已花)。"""
        return max(0.0, min(self.balance, self.day_cap(daily, factor) - self.spent_today))

    # ── 花 ──
    def charge(self, cost: float, daily: float, factor: float = 3.0,
               what: str = "") -> tuple[bool, str]:
        left = self.spendable(daily, factor)
        if cost > left + 1e-9:
            return False, f"{self.actor} 今天还能花 {left:.3f} 元，这一步要 {cost:.3f} 元"
        self.balance -= cost
        self.spent_today += cost
        self.spent_week += cost
        self.entries.append((what or "支出", -cost))
        return True, "ok"

    # ── 周末清零 ──
    def week_reset(self) -> None:
        self.entries.append(("周末清零", -self.balance))
        self.balance = 0.0
        self.spent_week = 0.0
        self.spent_today = 0.0

    # ── 给人看 / 给她看 ──
    def statement(self, daily: float, factor: float = 3.0,
                  accrue_cap_days: int = 5) -> str:
        """完整的账户视图。这就是"像电子账户一样可见"的那一面。"""
        cap = daily * factor
        pct = 0 if cap <= 0 else min(1.0, self.spent_today / cap)
        return "\n".join([
            f"额度账户  {self.actor}",
            f"  余额      {self.balance:.3f} 元   {bar(pct)}",
            f"  今日可花  {self.spendable(daily, factor):.3f} 元"
            f"（日上限 {cap:.3f}，已花 {self.spent_today:.3f}）",
            f"  每日入账  {daily:.3f} 元 · 攒额上限 {daily * accrue_cap_days:.3f} 元"
            f" · 周日 24:00 清零",
            f"  本周累计  {self.spent_week:.3f} 元",
        ])

    def hint(self, daily: float, factor: float = 3.0) -> str:
        """给她看的一行提示条。**十几个 token**，够她知道自己还有多少余地。

        刻意压短：它每次开口都出现，长一个字就是常年多花的钱。
        """
        cap = daily * factor
        pct = 0.0 if cap <= 0 else min(1.0, self.spent_today / cap)
        return (f"额度{bar(pct, 8)}{self.spendable(daily, factor):.1f}"
                f"/{cap:.1f}元")


def bar(fraction: float, width: int = 8) -> str:
    """进度条。用方块字符，比百分比更省 token 也更直观。"""
    f = max(0.0, min(1.0, fraction))
    full = int(round(f * width))
    return "[" + "█" * full + "░" * (width - full) + f"] {f * 100:.0f}%"


def new_week_account(actor: str, daily: float, cap: float) -> Account:
    """周一：清零之后立刻入账当天的那一份——所以**永远不会出现余额 0 还要等**。"""
    a = Account(actor=actor)
    a.week_reset()
    a.accrue(daily, cap)
    return a

@dataclass(frozen=True)
class Activity:
    """一次自主活动。★ 这里**只有元数据**，没有内容。

    她读了什么、写了什么、想了什么，属于 self 域的记忆节点，
    不进这张表——也就不进 `gits status`、不进日志展示。
    """

    actor: str
    ts_start: int
    ts_end: int
    kind: str            # 'browse' | 'read' | 'write' | 'script' | 'experiment' | 'peer' | 'idle'
    cny: float
    outputs: int         # 产出几个节点/脚本（只数个数，不看是什么）
    note: str = ""       # 只允许写"失败原因"这类运维信息
