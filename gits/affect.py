"""情绪：极坐标网格上的概率分布，不是一个心情词。

空间用 Plutchik 圆盘——它同时是**分类**和**度量**：

    角度 = 8 种基本情绪（joy/trust/fear/surprise/sadness/disgust/anger/anticipation）
    半径 = 3 档强度（低/中/高）
    24 格

选它而不是「valence × arousal 的二维格子」，理由很实际：
情绪词是有名字的（"生气"不是"v=-0.6,a=0.7"），有名字才能**说出来**、才能进 B 段、
才能被人检查。而圆形几何保留了度量：相邻格子相似，对面格子相反。

为什么是**分布**而不是一个标签：单条消息的判定噪声比信号大。
但分布的形态是稳的——哪几格、有多集中、有多少证据。
一个词会把噪声伪装成结论，一个分布不会。

    「她难过」            ← 无法核对
    「24 次观察，倾向 sadness(0.35)，浓度 0.4」  ← 可以核对，也知道自己知道得少

还有一块**不属于圆盘**的证据：委屈、心酸、憋屈没有干净的 Plutchik 归属，
硬塞进最近的格子就是编。所以单独记"哪一格都不是"。
"""

from __future__ import annotations

import math

from gits.db import DB, Writer

#: 8 个角度，按圆盘顺序排（对面 = 反义）
ANGLES = ("joy", "trust", "fear", "surprise",
          "sadness", "disgust", "anger", "anticipation")
INTENSITIES = (1, 2, 3)

#: 给渲染用的中文名。分档是因为"生气"和"暴怒"不是一回事。
ZH = {
    ("joy", 1): "轻快", ("joy", 2): "高兴", ("joy", 3): "雀跃",
    ("trust", 1): "放心", ("trust", 2): "信任", ("trust", 3): "托付",
    ("fear", 1): "不安", ("fear", 2): "害怕", ("fear", 3): "惊恐",
    ("surprise", 1): "意外", ("surprise", 2): "吃惊", ("surprise", 3): "震惊",
    ("sadness", 1): "低落", ("sadness", 2): "难过", ("sadness", 3): "崩溃",
    ("disgust", 1): "不适", ("disgust", 2): "反感", ("disgust", 3): "恶心",
    ("anger", 1): "不爽", ("anger", 2): "生气", ("anger", 3): "暴怒",
    ("anticipation", 1): "留意", ("anticipation", 2): "期待", ("anticipation", 3): "急切",
}

#: 遗忘时间常数（小时）。情绪是**小时级**的——三天前的气早该散了。
TAU_HOURS = 6.0
#: Dirichlet 平滑的总伪计数。24 格摊开，一条观察不足以压倒先验。
PRIOR_TOTAL = 2.0
#: 先验摊在 24 格 + 1 个 unknown 桶上——漏掉 unknown 那一份，总和就不是 1 了
BUCKETS = len(ANGLES) * len(INTENSITIES) + 1


def decay(now: int, last: int | None, *, tau_hours: float = TAU_HOURS) -> float:
    if not last:
        return 1.0
    dt_hours = max(0, now - int(last)) / 3_600_000
    return math.exp(-dt_hours / tau_hours)


def observe(db: DB, actor: str, *, angle: str | None, intensity: int = 2,
            ts: int, w: float = 1.0) -> None:
    """记一次观测。**angle 不认识就记进 unknown**，不猜最近的格子。"""
    if angle not in ANGLES or intensity not in INTENSITIES:
        angle, intensity = None, 0
    with Writer(db).txn() as c:
        if angle is None:
            row = c.execute("SELECT weight, last_ts FROM affect_unknown"
                            " WHERE actor=?", (actor,)).fetchone()
            kept = float(row[0]) * decay(ts, row[1]) if row else 0.0
            c.execute("INSERT INTO affect_unknown(actor, weight, last_ts)"
                      " VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET"
                      " weight=?, last_ts=?",
                      (actor, kept + w, ts, kept + w, ts))
            return
        row = c.execute("SELECT weight, last_ts FROM affect"
                        " WHERE actor=? AND angle=? AND intensity=?",
                        (actor, angle, intensity)).fetchone()
        kept = float(row[0]) * decay(ts, row[1]) if row else 0.0
        c.execute("INSERT INTO affect(actor, angle, intensity, weight, last_ts)"
                  " VALUES(?,?,?,?,?) ON CONFLICT(actor, angle, intensity)"
                  " DO UPDATE SET weight=?, last_ts=?",
                  (actor, angle, intensity, kept + w, ts, kept + w, ts))


def distribution(db: DB, actor: str, *, now: int) -> dict:
    """此刻的情绪分布。

    返回 {cells: {(angle,intensity): p}, unknown: p_unknown, n: 有效观测数,
          clarity: 0..1, top: (angle,intensity) | None}

    **衰减在读的时候算**，和 edges 一样——所以同一个时刻问两次答案一样，
    也不受"什么时候重放过"影响。
    """
    rows = db.q("SELECT angle, intensity, weight, last_ts FROM affect"
                " WHERE actor=?", (actor,))
    counts = {}
    for r in rows:
        v = float(r["weight"]) * decay(now, r["last_ts"])
        if v > 1e-9:
            counts[(str(r["angle"]), int(r["intensity"]))] = v
    unk_row = db.one("SELECT weight, last_ts FROM affect_unknown WHERE actor=?",
                     (actor,))
    unk = (float(unk_row["weight"]) * decay(now, unk_row["last_ts"])) \
        if unk_row else 0.0

    n = sum(counts.values())
    total = n + unk + PRIOR_TOTAL
    if total <= 0:
        return {"cells": {}, "unknown": 0.0, "n": 0.0, "clarity": 0.0,
                "top": None}

    cells = {k: (v + PRIOR_TOTAL / BUCKETS) / total for k, v in counts.items()}
    # 没观测过的格子也有先验质量——它们不该在返回里"消失"，否则分布看着比实际确信
    for angle in ANGLES:
        for inten in INTENSITIES:
            cells.setdefault((angle, inten), PRIOR_TOTAL / BUCKETS / total)

    ps = [p for p in cells.values()] + [(unk + PRIOR_TOTAL / BUCKETS) / total]
    h = -sum(p * math.log(p) for p in ps if p > 0)
    hmax = math.log(len(ps))
    clarity = max(0.0, 1.0 - h / hmax) if hmax > 0 else 0.0
    top = max(cells, key=lambda k: cells[k]) if counts else None
    return {"cells": cells, "unknown": (unk + PRIOR_TOTAL / BUCKETS) / total,
            "n": n, "clarity": clarity, "top": top}


def line(db: DB, actor: str, *, now: int) -> str:
    """B 段那一行。**说分布，不说结论。**"""
    d = distribution(db, actor, now=now)
    if d["n"] < 1.0 or d["top"] is None:
        return ""
    if d["unknown"] > 0.35:
        return "最近的情绪说不太清。"
    angle, inten = d["top"]
    word = ZH.get((angle, inten), angle)
    if d["clarity"] < 0.25:
        return f"最近有点{word}，但很杂。"
    return f"最近偏{word}。"
