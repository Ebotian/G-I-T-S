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

from gits.db import DB

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


def _normalise(rows, now: int) -> list[tuple[str, float]]:
    vals = [(str(d), effective(float(w), last, now)) for d, w, last in rows]
    total = sum(v for _d, v in vals)
    if total <= 0:
        return []
    vals.sort(key=lambda kv: -kv[1])
    return [(d, v / total) for d, v in vals]


def mentions_of(db: DB, episode: str, *, now: int,
                kind: str = "mentions") -> list[tuple[str, float]]:
    """P(实体 | 这条记忆)：这次提到的东西里，各占多少分量。"""
    rows = db.q("SELECT dst, w, last_coact FROM edges"
                " WHERE src=? AND kind=?", (episode, kind))
    return _normalise([(r["dst"], r["w"], r["last_coact"]) for r in rows], now)


def sources_of(db: DB, entity: str, *, now: int,
               kind: str = "mentions") -> list[tuple[str, float]]:
    """P(记忆 | 实体)：关于它的记忆里，哪几条分量最大。"""
    rows = db.q("SELECT src, w, last_coact FROM edges"
                " WHERE dst=? AND kind=?", (entity, kind))
    return _normalise([(r["src"], r["w"], r["last_coact"]) for r in rows], now)
