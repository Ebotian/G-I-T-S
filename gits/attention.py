"""注意力漂移：趋同干预的唯一执行器。

三条约束，每一条都在挡一种具体的坏结果：

  **只漂注意力**   不改记忆、不改观点（peers.md §6.1）。漂的是"更容易注意到什么"，
                  像状态，不像信念。
  **限速**         控制回路不允许阶跃。哪怕已经严重趋同，也只能一天挪一点——
                  "她突然变了"本身就是一种失败。
  **有界 + 回归**   一年 × 2%/天会飘很远。硬界 [0.5×, 2×]，无推动时朝初始值回 1%。

反相是刻意的：同一话题上，配对的两个实例朝**相反**方向漂。
靠运气让它们不一样是不可靠的——差异要由结构保证。
"""

from __future__ import annotations

import json
import math

from gits.db import DB, Writer, now_ms

#: 每日基础漂移幅度（相对初始权重的比例）
BASE_RATE = 0.02
#: 限速：一天最多漂这么多
MAX_DAILY = 0.10
#: 上下界
LOW, HIGH = 0.5, 2.0
#: 无推动时每天朝初始值回归的比例
REVERT = 0.01
#: 每对每天最多挪几个话题（多了就不叫"缓慢"了）
TOPICS_PER_DAY = 2


def seed(db: DB, actor: str, weights: dict[str, float], *,
         ts: int | None = None) -> int:
    """初始偏好。**这是 seed 事件，和真实经历共用日志。**"""
    ts = ts or now_ms()
    with Writer(db).txn() as c:
        for topic, w in weights.items():
            c.execute(
                "INSERT INTO attention(actor_id, topic, weight, base_weight,"
                " updated_ts) VALUES(?,?,?,?,?)"
                " ON CONFLICT(actor_id, topic) DO NOTHING",
                (actor, topic, float(w), float(w), ts))
    Writer(db).append_event(
        source="seed", kind="attention", ext_id=f"attention-{actor}-{ts}",
        ts=ts, recv_ts=ts, scope=f"self:{actor}",
        payload=json.dumps(weights, ensure_ascii=False).encode())
    return len(weights)


def weights(db: DB, actor: str) -> dict[str, float]:
    return {str(r["topic"]): float(r["weight"])
            for r in db.q("SELECT topic, weight FROM attention WHERE actor_id=?",
                          (actor,))}


def _clamp(w: float, base: float) -> float:
    return max(base * LOW, min(base * HIGH, w))


def step(db: DB, *, pair: tuple[str, str], pressure: float = 0.0,
         now: int | None = None) -> dict[str, dict[str, float]]:
    """漂一天。`pressure` 是重叠加压（0..1），只影响**幅度**，不影响方向。

    返回 {actor: {topic: delta}}，调用方负责记账。**没有这一步就悄悄改变了行为**，
    而一年后没人能解释她为什么变了。
    """
    now = now or now_ms()
    a, b = pair
    rate = min(MAX_DAILY, BASE_RATE * (1.0 + 4.0 * max(0.0, pressure)))
    wa, wb = weights(db, a), weights(db, b)
    shared = sorted(set(wa) & set(wb))[:TOPICS_PER_DAY]

    deltas: dict[str, dict[str, float]] = {a: {}, b: {}}
    with Writer(db).txn() as c:
        for topic in shared:
            base_a = float(db.scalar(
                "SELECT base_weight FROM attention WHERE actor_id=? AND topic=?",
                (a, topic)))
            base_b = float(db.scalar(
                "SELECT base_weight FROM attention WHERE actor_id=? AND topic=?",
                (b, topic)))
            # 反相：一个涨，另一个就跌。差异不靠运气。
            da = base_a * rate
            db_ = base_b * rate
            new_a = _clamp(wa[topic] + da, base_a)
            new_b = _clamp(wb[topic] - db_, base_b)
            c.execute("UPDATE attention SET weight=?, updated_ts=?"
                      " WHERE actor_id=? AND topic=?", (new_a, now, a, topic))
            c.execute("UPDATE attention SET weight=?, updated_ts=?"
                      " WHERE actor_id=? AND topic=?", (new_b, now, b, topic))
            deltas[a][topic] = round(new_a - wa[topic], 6)
            deltas[b][topic] = round(new_b - wb[topic], 6)

        # 没被推动的话题慢慢回到初始值——免得被某次偶然永久推歪
        for actor in (a, b):
            for topic in wa if actor == a else wb:
                if topic in shared:
                    continue
                row = db.one("SELECT weight, base_weight FROM attention"
                             " WHERE actor_id=? AND topic=?", (actor, topic))
                if row is None:
                    continue
                w, base = float(row["weight"]), float(row["base_weight"])
                new = _clamp(w + (base - w) * REVERT, base)
                if abs(new - w) < 1e-9:
                    continue
                c.execute("UPDATE attention SET weight=?, updated_ts=?"
                          " WHERE actor_id=? AND topic=?", (new, now, actor, topic))
                deltas[actor][topic] = round(new - w, 6)

    if any(deltas.values()):
        Writer(db).append_event(
            source="drift", kind="drift", ext_id=f"drift-{a}-{b}-{now}",
            ts=now, recv_ts=now,
            scope=f"self:{a}",
            payload=json.dumps({"pair": [a, b], "rate": round(rate, 4),
                                "pressure": round(pressure, 3),
                                "delta": deltas}, ensure_ascii=False).encode())
    return deltas


def overlap_pressure(overlap: float | None, *, baseline: float = 0.15,
                     ceiling: float = 0.45) -> float:
    """把 3-gram 重合率折成 0..1 的"该分开了"程度。

    不传重合率就不施压——**不知道就别动**，比乱动好。
    """
    if overlap is None:
        return 0.0
    x = (overlap - baseline) / max(1e-9, ceiling - baseline)
    return float(min(1.0, max(0.0, x)))
