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
    """当天已用的账。日清，**不结转**——结转会让她攒额度等大事，
    然后为了"用掉"而行动，那就变成额度驱动了。"""

    day: str
    used: dict[tuple[str, str], float] = field(default_factory=dict)

    def spent(self, actor: str, kind: str) -> float:
        return self.used.get((actor, kind), 0.0)

    def remaining(self, actor: str, kind: str, allow: Allowance) -> float:
        return max(0.0, allow.units.get(kind, 0.0) - self.spent(actor, kind))

    def charge(self, actor: str, kind: str, amount: float,
               allow: Allowance) -> tuple[bool, str]:
        """扣额度。

        **总额是硬的，分项是软的。** 每个 kind 各给一份"如果只花这一类能花多少"，
        但它们加起来必然超过日额度——真实用法里输入输出比例是未知的，
        所以真正卡住的是**这个人的日额度**，分项只做提示。
        """
        left = self.remaining(actor, kind, allow)
        if amount > left:
            return False, f"{actor} 的 {kind} 不够（要 {amount:.0f}，剩 {left:.0f}）"

        cost = amount * UNIT_PRICE.get(kind, 0.0)
        if self.spent_cny(actor) + cost > allow.cny + 1e-9:
            return False, (f"{actor} 今天只剩 {allow.cny - self.spent_cny(actor):.3f} 元，"
                           f"这一步要 {cost:.3f} 元")
        self.used[(actor, kind)] = self.spent(actor, kind) + amount
        return True, "ok"

    def spent_cny(self, actor: str | None = None) -> float:
        """今天实际花了多少钱。按与额度派生**同一张**单价表换算，否则对不上账。"""
        return sum(amount * UNIT_PRICE.get(kind, 0.0)
                   for (who, kind), amount in self.used.items()
                   if actor is None or who == actor)


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
