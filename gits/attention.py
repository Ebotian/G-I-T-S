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
#: 上下界（weight 相对锚点的倍数）
LOW, HIGH = 0.5, 2.0
#: 锚点自己的取值区间
WEIGHT_RANGE = (0.1, 5.0)
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
         now: int | None = None, reestimate_anchors: bool = True,
         ) -> dict[str, dict[str, float]]:
    """漂一天。`pressure` 是重叠加压（0..1），只影响**幅度**，不影响方向。

    返回 {actor: {topic: delta}}，调用方负责记账。**没有这一步就悄悄改变了行为**，
    而一年后没人能解释她为什么变了。
    """
    now = now or now_ms()
    a, b = pair
    # 先让锚点跟着真实数据走一步，再谈漂移。
    # 顺序有意义：锚点决定漂移围绕的**中心**，中心没更新就漂是白漂。
    if reestimate_anchors:
        reestimate(db, a, now=now)
        reestimate(db, b, now=now)
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


# ── 锚点从数据里估，不是写死的魔法数字 ──────────────────────
#
# 种子给的 1.2 只是**初始锚点**。之后每天锚点朝"这个话题最近真的被提到多少次"
# 挪一点；三个月没人提，它就回到中性 1.0——偏好自己消失。
#
# 同时，从没被种过但一直被提到的话题会**自己长出来**：
# 否则注意力就被冻在种子那一刻，而生活早就换话题了。

OBSERVE_LAMBDA = 0.05     # 每天锚点朝观测值挪多少
NEUTRAL = 1.0             # 没被提到 = 中性，不是 0（0 会让它永不可能被想起）
WINDOW_DAYS = 7           # 观测窗口
#: 收缩强度：相当于「几个伪观测」。n=1 时置信 0.25，n=9 时 0.75。
#: 少样本起点，宁可慢一点，也别被一次提及推歪。
SHRINK_K = 3.0
MIN_MENTIONS = 3          # 从数据里新建话题的最低门槛
MAX_TOPICS = 32
ANCHOR_EPS = 0.01         # 挪动小于这个就不记事件


def _mentions(db: DB, *, now: int,
              window_days: int = WINDOW_DAYS) -> dict[str, float]:
    """最近窗口里，每个实体被提到了**多少分量**（不是多少条）。

    早先是 count(*) 数行数，完全不管 edges.w：10 次深谈和 10 次顺嘴一提
    权重一样。现在按带遗忘的强度求和（graph.py）。
    """
    from gits.graph import effective

    since = now - window_days * 86_400_000
    rows = db.q(
        "SELECT c.content AS name, e.w, e.last_coact FROM edges e"
        " JOIN nodes d ON d.id = e.dst AND d.kind = 'entity'"
        " JOIN node_content c ON c.id ="
        "   (SELECT max(id) FROM node_content WHERE node_id = d.id)"
        " WHERE e.kind = 'mentions' AND e.t_created > ?", (since,))
    out: dict[str, float] = {}
    for r in rows:
        v = effective(float(r["w"]), r["last_coact"], now)
        out[str(r["name"])] = out.get(str(r["name"]), 0.0) + v
    return out


def _target(n: float, avg: float) -> float:
    """提及分量 -> 目标锚点。**样本少就少动。**

    先用相对频率算一个「原始目标」，再按证据量往中性 1.0 收缩：

        置信 = n / (n + K)
        目标 = 1 + (原始目标 − 1) × 置信

    这是共轭先验下后验均值的形状：K 相当于「几个伪观测」。
    理由很实际——现在是少样本起点，一次提及不该把锚点推满；
    而 w 本身已经带衰减，所以这里的 n 是**有效样本量**，不是累计次数。
    """
    if n <= 0:
        return NEUTRAL
    lo, hi = WEIGHT_RANGE
    raw = 1.0 + 2.0 * (n / max(1e-9, avg) - 1.0)
    conf = n / (n + SHRINK_K)
    return float(min(hi, max(lo, 1.0 + (raw - 1.0) * conf)))


def reestimate(db: DB, actor: str, *, now: int) -> dict[str, float]:
    """把锚点朝观测值挪一步。返回变动量（只含挪动过的）。"""
    counts = _mentions(db, now=now)
    rows = db.q("SELECT topic, weight, base_weight FROM attention WHERE actor_id=?",
                (actor,))
    if not rows and not counts:
        return {}

    live = {str(r["topic"]): (float(r["weight"]), float(r["base_weight"]))
            for r in rows}
    # 平均值只在"确实被提到过"的话题上算，免得一堆 0 把它稀释成什么都看不见
    observed = [counts[t] for t in live if counts.get(t, 0) > 0]
    if not observed:
        observed = [c for c in counts.values() if c > 0]
    avg = max(1.0, sum(observed) / len(observed)) if observed else 1.0

    moved: dict[str, float] = {}
    with Writer(db).txn() as c:
        for topic, (w, base) in live.items():
            tgt = _target(counts.get(topic, 0), avg)
            new_base = round(base + OBSERVE_LAMBDA * (tgt - base), 6)
            # **一直挪，别在快到的时候停下。**早先写成"步子小于 0.01 就不动"，
            # 结果锚点永远停在 1.19 回不到 1.0——指数衰减的尾巴正好落在阈值上。
            # 阈值只用来决定"值不值得记一条事件"。
            c.execute("UPDATE attention SET base_weight=?, updated_ts=?"
                      " WHERE actor_id=? AND topic=?", (new_base, now, actor, topic))
            if abs(new_base - base) >= ANCHOR_EPS:
                moved[topic] = round(new_base - base, 6)

        # 从没种过、但一直被提到的话题：自己长出来
        fresh = [(t, n) for t, n in counts.items()
                 if t not in live and n >= MIN_MENTIONS]
        fresh.sort(key=lambda kv: -kv[1])
        for topic, n in fresh:
            if len(live) + len(moved) + len(fresh) > MAX_TOPICS:
                break
            tgt = _target(n, avg)
            c.execute(
                "INSERT INTO attention(actor_id, topic, weight, base_weight,"
                " updated_ts) VALUES(?,?,?,?,?)"
                " ON CONFLICT(actor_id, topic) DO NOTHING",
                (actor, topic, tgt, tgt, now))
            moved[topic] = round(tgt - 0.0, 6)

    if moved:
        Writer(db).append_event(
            source="attention", kind="anchor", ext_id=f"anchor-{actor}-{now}",
            ts=now, recv_ts=now, scope=f"self:{actor}",
            payload=json.dumps({"delta": moved}, ensure_ascii=False).encode())
    return moved
