"""上下文装配：A / B / C 三段。

DESIGN §5.4。这里的每一条约束都是**用类型签名或测试**保证的，不是靠自律：

    A 段一天一变，必须逐字稳定   → render_a(seed) 的签名里没有时间
    B 段按小时快照              → bucket() 把 now 折叠成整数小时
    C 段每轮都变                → 唯一可以塞"现在"的地方

缓存命中价是未命中的 1/50（VERIFIED §1），所以 A 段里出现一个时间戳，
就等于把整条 prompt 后面的缓存全废掉。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: 中文大致 1.5 字/token。是估算，真实值以 API 返回的 usage 为准。
CHARS_PER_TOKEN = 1.5


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


@dataclass(frozen=True)
class PersonaSeed:
    """A 段：她是谁。一天只换一次（§5.5 自我叙事）。"""

    narrative: str
    version: int
    style_rules: tuple[str, ...] = (
        "短句优先。能停就停。",
        "不解释自己在做什么，不用括号补充。",
        "不用表格、不用分点、不用加粗。",
        "不确定就说不知道，不编。",
    )
    output_contract: str = (
        "只输出你要说的话本身。不要有元叙述、不要描述语气、不要说自己是 AI。"
    )


@dataclass(frozen=True)
class RelationSnapshot:
    """B 段：我们此刻是什么关系。按小时快照，所以一小时内逐字不变。"""

    bucket: int
    summary: str
    hubs: tuple[str, ...]
    phase: str


@dataclass(frozen=True)
class TurnContext:
    """C 段：这一轮独有的东西。"""

    stimulus: str
    recalled: tuple[str, ...] = ()
    recent: tuple[tuple[str, str], ...] = ()  # (role, text)


@dataclass(frozen=True)
class Prompt:
    a: str
    b: str
    c: str
    text: str

    @property
    def a_hash(self) -> str:
        return hashlib.sha256(self.a.encode()).hexdigest()

    def budget(self) -> dict[str, int]:
        return {
            "a": estimate_tokens(self.a),
            "b": estimate_tokens(self.b),
            "c": estimate_tokens(self.c),
            "total": estimate_tokens(self.text),
        }


def render_a(seed: PersonaSeed) -> str:
    """★ 签名里没有时间参数——这是不可破坏的约定，不是巧合。

    A 段只允许依赖 PersonaSeed。想往里塞"当前时间""本轮消息"的人，
    会发现这个函数拿不到它们。
    """
    rules = "\n".join(f"- {r}" for r in seed.style_rules)
    return (
        "# 你是谁\n"
        f"{seed.narrative.strip()}\n\n"
        "# 你说话的样子\n"
        f"{rules}\n\n"
        "# 输出\n"
        f"{seed.output_contract.strip()}\n"
    )


def bucket(now_ms: int, hours: int = 1) -> int:
    """把 now 折叠成整数小时间隔。B 段只认它，不认 now。"""
    if hours <= 0:
        raise ValueError("hours 必须为正")
    return now_ms // (hours * 3600_000)


def render_b(snap: RelationSnapshot) -> str:
    hubs = "、".join(snap.hubs) if snap.hubs else "（还没有什么放不下的事）"
    return (
        f"# 我们现在\n{snap.summary.strip()}\n"
        f"你最近在意：{hubs}\n"
        f"此刻的状态：{snap.phase}\n"
    )


def render_c(ctx: TurnContext) -> str:
    parts = []
    if ctx.recent:
        parts.append("# 刚才")
        for role, text in ctx.recent:
            parts.append(f"{role}：{text}")
    if ctx.recalled:
        parts.append("# 你想起来的事")
        parts.extend(f"- {r}" for r in ctx.recalled)
    parts.append("# 现在")
    parts.append(ctx.stimulus.strip())
    return "\n".join(parts) + "\n"


def assemble(seed: PersonaSeed, snap: RelationSnapshot, ctx: TurnContext) -> Prompt:
    a, b, c = render_a(seed), render_b(snap), render_c(ctx)
    # 顺序就是缓存布局：稳定前缀在前，易变在后（DESIGN §5.4）
    return Prompt(a=a, b=b, c=c, text=a + b + c)


def status_line(*, backlog: int, s_user: float | None, reciprocity: float | None,
                quota_left: int | None = None, stale: bool = False) -> str:
    """`gits status` 那一行（§15.7 / §14.0）。墙钟不出现，承诺线单独标。"""
    bits = []
    bits.append(f"积压 {backlog} 件")
    bits.append("s_user=?" if s_user is None else f"s_user={s_user:.2f}")
    bits.append("reciprocity=fallback" if reciprocity is None else f"reciprocity={reciprocity:.2f}")
    if quota_left is not None:
        bits.append(f"配额剩 {quota_left}")
    if stale:
        bits.append("只读（副本过期）")
    return " · ".join(bits)
