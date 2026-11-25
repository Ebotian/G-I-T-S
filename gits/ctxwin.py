"""上下文窗口：什么时候该换一扇新的。

缓存有"代"。同一个窗口里，前缀逐字不变 → 每轮都命中；
换一扇窗 → 下一个请求全价重算。所以这不是"清理"，是**换代价**：
一次全价输入，换回接下来几十轮的命中。

三个触发条件，按优先级（docs/research/context-budget.md）：

  1. 缓存过期  同窗口前缀没动，却连续 2 次 0 命中——只能靠推断（TTL 不可读）
  2. 命中率崩  最近 20 次 < 50%，说明形状坏了，立刻换，不等它满
  3. 太长      80% 强制、60% 闲时（要闲得够久才做，用 idle.p10）
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.db import DB, Writer, now_ms
from gits.idle import Idle

#: 每轮 prompt 的预算。**按最坏情况（全未命中）可承受来定，不是按省 token 定。**
#: 12K × 1 元/M × 150 轮 = 1.8 元/天 是天花板；全命中时只花 0.036 元。
BUDGET_TOKENS = 12_000
SOFT = 0.60          # 闲时整理
HARD = 0.80          # 强制整理（要告诉她）
LOW_HIT = 0.50       # 命中率低于此值立刻换窗
SAMPLE = 20          # 命中率看最近多少次
ZERO_STREAK = 2      # 连续几次 0 命中判定缓存过期


@dataclass(frozen=True)
class Plan:
    action: str        # append | compact_idle | compact_force | new_window | open
    reason: str
    tell_user: bool = False      # 换窗会不会让她"接不上"


def current(db: DB) -> dict | None:
    row = db.one("SELECT * FROM ctx_windows WHERE closed_ts IS NULL"
                 " ORDER BY id DESC LIMIT 1")
    return dict(row) if row else None


def new_window(db: DB, *, ts: int | None = None, reason: str = "") -> int:
    """换一扇新窗。**边界一定要写**：不写 first_event_id，下一轮查询
    会从 0 开始读，把旧窗口的历史又读回来——换了等于没换。"""
    ts = ts or now_ms()
    cur = current(db)
    if cur:
        close(db, cur["id"], reason=reason or "换窗", ts=ts)
    edge = db.scalar("SELECT max(id) FROM events") or 0
    return open_window(db, ts=ts, first_event_id=int(edge) + 1)


def open_window(db: DB, *, ts: int | None = None,
                first_event_id: int | None = None) -> int:
    cur = current(db)
    if cur:
        close(db, cur["id"], reason="重开", ts=ts)
    with Writer(db).txn() as c:
        return int(c.execute(
            "INSERT INTO ctx_windows(started_ts, first_event_id) VALUES(?,?)",
            (ts or now_ms(), first_event_id)).lastrowid)


def close(db: DB, window_id: int, *, reason: str, ts: int | None = None) -> None:
    with Writer(db).txn() as c:
        c.execute("UPDATE ctx_windows SET closed_ts=?, close_reason=?"
                  " WHERE id=? AND closed_ts IS NULL",
                  (ts or now_ms(), reason, window_id))


def record(db: DB, window_id: int, *, tokens: int, hit_tokens: int,
           event_id: int | None = None) -> None:
    """记一轮的账。**tokens 用 API 返回的 prompt_tokens**，不估算。"""
    zero = 1 if hit_tokens <= 0 else 0
    with Writer(db).txn() as c:
        c.execute(
            "UPDATE ctx_windows SET turns=turns+1, tokens=?, hit_tokens=?,"
            " zero_streak = CASE WHEN ? THEN zero_streak+1 ELSE 0 END,"
            " last_event_id=COALESCE(?, last_event_id) WHERE id=?",
            (tokens, hit_tokens, zero, event_id, window_id))


def hit_rate(db: DB, *, sample: int = SAMPLE) -> float | None:
    rows = db.q(
        "SELECT tokens, hit_tokens FROM ctx_windows WHERE turns > 0"
        " ORDER BY id DESC LIMIT ?", (sample,))
    tot = sum(int(r["tokens"]) for r in rows)
    if not tot:
        return None
    return sum(int(r["hit_tokens"]) for r in rows) / tot


def plan(db: DB, *, now: int | None = None, tokens: int | None = None,
         idle: Idle | None = None, compact_ms: int = 20_000) -> Plan:
    """该不该换窗。**顺序就是优先级，先命中、再长度。**"""
    now = now or now_ms()
    win = current(db)
    if win is None:
        return Plan("open", "还没有窗口")

    tokens = int(win["tokens"] if tokens is None else tokens)

    # 1. 缓存过期：前缀没动却连续 0 命中。TTL 读不到，只能这样推断。
    if int(win["zero_streak"]) >= ZERO_STREAK:
        return Plan("new_window", f"连续 {win['zero_streak']} 次 0 命中，判定缓存过期")

    # 2. 命中率崩了：形状坏了，跟长度无关
    hr = hit_rate(db)
    if hr is not None and hr < LOW_HIT and int(win["turns"]) >= 5:
        return Plan("compact_force", f"最近命中率 {hr:.0%} < {LOW_HIT:.0%}")

    # 3. 太长
    if tokens >= BUDGET_TOKENS * HARD:
        return Plan("compact_force",
                    f"{tokens}/{BUDGET_TOKENS} token 超过 {HARD:.0%}，强制整理",
                    tell_user=True)
    if tokens >= BUDGET_TOKENS * SOFT:
        if idle is None:
            return Plan("append", f"{tokens}/{BUDGET_TOKENS} 该整理了，但不知道你闲不闲")
        if idle.at_least(compact_ms):
            return Plan("compact_idle",
                        f"空闲下界 {idle.p10_ms // 1000}s > 整理 {compact_ms // 1000}s×1.5")
        return Plan("append", f"{tokens}/{BUDGET_TOKENS} 该整理，但你随时可能回来")

    return Plan("append", f"{tokens}/{BUDGET_TOKENS}")

