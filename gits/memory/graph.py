"""图上的统计沉淀：不是"记了什么"，是"什么经常一起出现"。

`edges` 一直是个稀疏矩阵（src × dst），但从来没人读过 `w`——
写入是纯计数 `w = w + 1`，永不衰减。这里把它变成**带遗忘的计数**：

    w ← w · e^(−Δt/τ) + 1        （每次共现，Δt 是距上次共现的时间）
    P(dst|src) = w / Σ w          （读的时候行归一化）

两条纪律：

  **成员资格不衰减。**行存在就是"共现过"，永久有效；衰减的只有强度。
  否则 `w` 掉到 0.01 时簇会静默消失，漏斗再也不压它——而且不会报错。

  **存的是"上次共现那一刻的值"，衰减在读的时候算。**
  把 now 写进存储的话，`project.restore` 就不再确定：同一个日志，
  不同时间重放会得到不同的图。
"""

from __future__ import annotations

import math
import sqlite3

from gits.core.db import DB

DAY = 86_400_000
#: 遗忘时间常数。和 FSRS 一个量级——它们描述的是同一件事的不同侧面。
TAU_DAYS = 30.0


def decay(now: int, last: int | None, *, tau_days: float = TAU_DAYS) -> float:
    """从 last 到现在衰减了多少。没有 last 就当没衰减。"""
    if not last:
        return 1.0
    dt = max(0, now - int(last))
    return math.exp(-dt / (tau_days * DAY))


def add_edge(conn: sqlite3.Connection, *, src: str, dst: str, kind: str,
             ts: int, w: float = 1.0) -> None:
    """记一次共现。**带遗忘**：先把旧值按时间衰减，再加这次。

    只用 ts（事件时间），不看 now——所以重放是确定的。
    """
    row = conn.execute(
        "SELECT w, last_coact FROM edges WHERE src=? AND dst=? AND kind=?",
        (src, dst, kind)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO edges(src, dst, w, kind, t_created, last_coact)"
            " VALUES(?,?,?,?,?,?)", (src, dst, w, kind, ts, ts))
        return
    kept = float(row[0]) * decay(ts, row[1])
    conn.execute(
        "UPDATE edges SET w=?, last_coact=? WHERE src=? AND dst=? AND kind=?",
        (kept + w, ts, src, dst, kind))


def effective(w: float, last_coact: int | None, now: int,
             *, tau_days: float = TAU_DAYS) -> float:
    """那次共现记下来的强度，衰减到现在是多少。"""
    return float(w) * decay(now, last_coact, tau_days=tau_days)


def strengths(db: DB, *, now: int, kind: str = "mentions") -> dict[str, float]:
    """每个 dst 现在的总强度。按 dst 求和，行归一化前的分子。"""
    out: dict[str, float] = {}
    for r in db.q("SELECT dst, w, last_coact FROM edges WHERE kind=?", (kind,)):
        out[str(r["dst"])] = out.get(str(r["dst"]), 0.0) + effective(
            float(r["w"]), r["last_coact"], now)
    return out


#: Beta 先验的两个伪计数。0.5 = Jeffreys，含义是「一次观察只算一半证据」。
#: 少样本时它把估计拉向 0.5（不确定），样本多了才让数据说话。
ALPHA = 0.5
BETA = 0.5


def association(w_eff: float, *, alpha: float = ALPHA,
                beta: float = BETA) -> float:
    """这条记忆和这个实体有多强关联，**后验均值**：

        (w + α) / (w + α + β)

    为什么不是行归一化成概率：一件事同时提到 A 和 B，并不意味着它们在
    竞争一个名额。所以这里的量不是「分布」，而是「关联强度」。

    参考值（α=β=0.5）：

        w=0   → 0.50   没证据 = 不知道，不是「没有」
        w=1   → 0.75   一次观察不是强证据
        w=3   → 0.875
        w=10  → 0.955  数据多了就接近 MLE
    """
    return (w_eff + alpha) / (w_eff + alpha + beta)


def mentions_of(db: DB, episode: str, *, now: int,
                kind: str = "mentions") -> list[tuple[str, float]]:
    """这条记忆提到的实体，各自和它有多强关联（后验均值，降序）。"""
    rows = db.q("SELECT dst, w, last_coact FROM edges"
                " WHERE src=? AND kind=?", (episode, kind))
    vals = [(str(r["dst"]),
             association(effective(float(r["w"]), r["last_coact"], now)))
            for r in rows]
    return sorted(vals, key=lambda kv: -kv[1])


def sources_of(db: DB, entity: str, *, now: int,
               kind: str = "mentions") -> list[tuple[str, float]]:
    """关于这个实体的记忆，各自的关联强度。**衰减之后谁还算数，一眼能看出来。**"""
    rows = db.q("SELECT src, w, last_coact FROM edges"
                " WHERE dst=? AND kind=?", (entity, kind))
    vals = [(str(r["src"]),
             association(effective(float(r["w"]), r["last_coact"], now)))
            for r in rows]
    return sorted(vals, key=lambda kv: -kv[1])
