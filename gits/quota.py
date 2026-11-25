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
    #: 单日上限 = 日额度 × 这个倍数。**防止一天把整周花光**——
    #: 结转提供了自由度，也必须配一个速率上限，否则"周额度"就会被当成"当日额度"。
    burst_factor: float = 2.5


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
class Ledger:
    """周账本 + 日上限。结转是**算出来的**，不是搬来搬去的。

    口径（DESIGN §18.6）：

        周额度 = 日额度 × 7
        今天能花 = min(日上限, 周额度 − 本周此前已花)
        周一清零

    为什么用"周锅"而不是"每日余额搬来搬去"：**搬余额会漏**。
    第一天花掉 2.5（日上限），但那天只"挣"了 0.667，余额就变成负数——
    负余额要么被忽略（等于可以透支整周），要么变成欠账（等于惩罚）。
    用周锅一算就干净：**能花的永远不超过本周剩下的**。

    日上限（`burst_factor`）也不能省：只有周额度的话，一周的钱第一天就能花光，
    那等于没有日额度。
    """

    week: str
    day: str
    #: 本周**此前**已经花掉的（不含今天），按人记
    spent_before: dict[str, float] = field(default_factory=dict)
    #: 今天已扣的量，按 (人, 类型) 记
    used: dict[tuple[str, str], float] = field(default_factory=dict)

    # ── 账 ──
    def spent(self, actor: str, kind: str) -> float:
        return self.used.get((actor, kind), 0.0)

    def spent_cny(self, actor: str | None = None) -> float:
        """今天实际花了多少钱。按与额度派生**同一张**单价表换算，否则对不上账。"""
        return sum(amount * UNIT_PRICE.get(kind, 0.0)
                   for (who, kind), amount in self.used.items()
                   if actor is None or who == actor)

    def weekly_budget(self, allow: Allowance, b: Budget) -> float:
        return allow.cny * b.week_days

    def limit_cny(self, actor: str, allow: Allowance, b: Budget) -> float:
        """今天能花的上限 = 日上限 与 本周剩余 取小。"""
        week_left = self.weekly_budget(allow, b) - self.spent_before.get(actor, 0.0)
        return max(0.0, min(day_cap(b, allow), week_left))

    def remaining_cny(self, actor: str, allow: Allowance, b: Budget) -> float:
        return max(0.0, self.limit_cny(actor, allow, b) - self.spent_cny(actor))

    def remaining(self, actor: str, kind: str, allow: Allowance) -> float:
        """分项余量。**只用于展示和估算，不参与准入。**"""
        return max(0.0, allow.units.get(kind, 0.0) - self.spent(actor, kind))

    # ── 扣 ──
    def charge(self, actor: str, kind: str, amount: float,
               allow: Allowance, b: Budget | None = None) -> tuple[bool, str]:
        """扣额度。

        **钱是唯一的硬约束。** 分项单位（抓取次数、输出 token）只是估算与展示，
        不参与准入——真实用法里"今天想多读点、少写点"是她的自由，
        按分项卡死等于替她决定用法。

        硬约束只有两条：**日上限**（防止一天花光整周）和**周额度**。
        """
        b = b or Budget()
        cost = amount * UNIT_PRICE.get(kind, 0.0)
        if cost > self.remaining_cny(actor, allow, b) + 1e-9:
            return False, (f"{actor} 今天只剩 {self.remaining_cny(actor, allow, b):.3f} 元，"
                           f"这一步要 {cost:.3f} 元")
        self.used[(actor, kind)] = self.spent(actor, kind) + amount
        return True, "ok"

    # ── 结转 ──
    def carry_out(self, actor: str, allow: Allowance, b: Budget) -> float:
        """今天没花完的，自然留在周锅里。**不会因为"没花"而扣分。**"""
        return self.remaining_cny(actor, allow, b)

    def tomorrow(self) -> "Ledger":
        """结转的实现就这一行：今天的支出并入"本周此前已花"。"""
        merged = dict(self.spent_before)
        for who in {w for (w, _k) in self.used}:
            merged[who] = self.spent_before.get(who, 0.0) + self.spent_cny(who)
        return Ledger(week=self.week, day=self.day + "+1", spent_before=merged)


def new_week(week: str, day: str) -> Ledger:
    """周一：周锅清零。这是唯一会"没收"额度的时刻，而且它是对称的——
    每个人每周一都从同一个起点开始。"""
    return Ledger(week=week, day=day, spent_before={})

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
