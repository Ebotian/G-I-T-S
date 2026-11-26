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

from gits.core.db import DB, Writer

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


# ── 连续转移：情绪在空间里是流动的，不是跳格 ──────────────────
#
# 一次观测落在某格只是推了一把。之后它自己走：
#
#   径向   3 → 2 → 1     强度先下来（暴怒 → 生气 → 不爽）
#   环向   向相邻两格扩散（生气 → 不爽，或和相邻情绪混在一起）
#
# 两个都用时间常数写闭式解，**不做步进**：
#
#   k = 1 − e^(−dt/τ)
#
# 步进的话结果会依赖读取频率——同一时刻问两次可能不一样。
#
# 速度和幅度由语境定：**越激动，走得越快**。
# arousal 直接从这个分布本身算（强度加权），不额外估一个量。

#: 径向与环向的基础时间常数（小时）。arousal=1 时减半。
TAU_RADIAL_H = 3.0
TAU_CIRCULAR_H = 2.0


def arousal(cells: dict[tuple[str, int], float]) -> float:
    """0..1：现在有多激动。强度加权，不需要额外估计。"""
    total = sum(cells.values())
    if total <= 0:
        return 0.0
    return sum(v * (inten - 1) / 2.0 for (_a, inten), v in cells.items()) / total


def evolve(cells: dict[tuple[str, int], float], dt_ms: int, *,
           aroused: float | None = None) -> dict[tuple[str, int], float]:
    """把状态往前推 dt。闭式解，不步进。

    顺序：先强度下移（径向），再向相邻角度扩散（环向）。
    两者都不改变总质量——只有 decay() 会。
    """
    if dt_ms <= 0:
        return dict(cells)
    hours = dt_ms / 3_600_000
    ar = arousal(cells) if aroused is None else aroused
    tau_r = TAU_RADIAL_H / (1.0 + ar)
    tau_c = TAU_CIRCULAR_H / (1.0 + ar)
    k_r = 1.0 - math.exp(-hours / tau_r)
    k_c = 1.0 - math.exp(-hours / tau_c)

    # 径向是 3→2→1 的**级联**，不能只挪一格：
    # 只挪一格的话，24 小时之后的质量还停在 2 档（因为落在 2 档的那部分
    # 在同一次调用里不会再往下走）。所以用 Erlang 的闭式解：
    #
    #   P(3→3) = a        P(3→2) = b·a        P(3→1) = 1 − a(1+b)
    #   P(2→2) = a        P(2→1) = 1 − a        （1 档是吸收态）
    a = math.exp(-hours / tau_r)
    b = hours / tau_r
    radial: dict[tuple[str, int], float] = {}
    for angle in ANGLES:
        m3 = cells.get((angle, 3), 0.0)
        m2 = cells.get((angle, 2), 0.0)
        m1 = cells.get((angle, 1), 0.0)
        radial[(angle, 3)] = m3 * a
        radial[(angle, 2)] = m3 * b * a + m2 * a
        radial[(angle, 1)] = m3 * (1.0 - a * (1.0 + b)) + m2 * (1.0 - a) + m1

    out: dict[tuple[str, int], float] = {}
    n = len(ANGLES)
    for (angle, inten), v in radial.items():
        if k_c <= 1e-9 or v <= 0:
            out[(angle, inten)] = out.get((angle, inten), 0.0) + v
            continue
        move = v * k_c
        out[(angle, inten)] = out.get((angle, inten), 0.0) + v - move
        i = ANGLES.index(angle)
        for j in ((i - 1) % n, (i + 1) % n):
            key = (ANGLES[j], inten)
            out[key] = out.get(key, 0.0) + move / 2.0
    return {k: v for k, v in out.items() if v > 1e-12}


def decay(dt_ms: int, *, tau_hours: float = TAU_HOURS) -> float:
    """纯衰减系数。情绪会散——这是它和记忆最大的区别。"""
    if dt_ms <= 0:
        return 1.0
    return math.exp(-(dt_ms / 3_600_000) / tau_hours)


def advance(cells: dict[tuple[str, int], float], dt_ms: int,
            unknown: float = 0.0) -> tuple[dict[tuple[str, int], float], float]:
    """把整个状态往前推 dt：先流动（径向+环向），再衰减。

    一个函数，两处用：observe() 加新观测之前推一次，distribution() 读之前推一次。
    两边用同一个函数，状态才会一致——分开写迟早会漂。
    """
    if dt_ms <= 0:
        return dict(cells), unknown
    moved = evolve(cells, dt_ms)
    k = decay(dt_ms)
    return ({c: v * k for c, v in moved.items() if v * k > 1e-12},
            unknown * k)


def _state(db: DB, actor: str) -> tuple[dict[tuple[str, int], float], int, float]:
    rows = db.q('SELECT angle, intensity, weight FROM affect WHERE actor=?',
                (actor,))
    cells = {(str(r['angle']), int(r['intensity'])): float(r['weight'])
             for r in rows}
    st = db.one('SELECT ts, unknown FROM affect_state WHERE actor=?', (actor,))
    if st is None:
        return cells, 0, 0.0
    return cells, int(st['ts']), float(st['unknown'])


def _store(db: DB, actor: str, cells: dict[tuple[str, int], float],
           unknown: float, ts: int) -> None:
    with Writer(db).txn() as c:
        c.execute('DELETE FROM affect WHERE actor=?', (actor,))
        for (angle, inten), v in cells.items():
            if v <= 1e-12:
                continue
            c.execute('INSERT INTO affect(actor, angle, intensity, weight)'
                      ' VALUES(?,?,?,?)', (actor, angle, inten, v))
        c.execute('INSERT INTO affect_state(actor, ts, unknown) VALUES(?,?,?)'
                  ' ON CONFLICT(actor) DO UPDATE SET ts=?, unknown=?',
                  (actor, ts, unknown, ts, unknown))


def observe(db: DB, actor: str, *, angle: str | None, intensity: int = 2,
            ts: int, w: float = 1.0) -> None:
    """记一次观测。**先把这个状态推到 ts，再加**——顺序反了就丢历史。

    angle 不认识就记进 unknown，不猜最近的格子（委屈、心酸、憋屈没有归属）。
    """
    cells, state_ts, unknown = _state(db, actor)
    cells, unknown = advance(cells, ts - state_ts, unknown)
    if angle in ANGLES and intensity in INTENSITIES:
        key = (str(angle), int(intensity))
        cells[key] = cells.get(key, 0.0) + w
    else:
        unknown += w
    _store(db, actor, cells, unknown, ts)


def distribution(db: DB, actor: str, *, now: int) -> dict:
    """此刻的情绪分布（后验均值，含 Dirichlet 先验）。"""
    cells, state_ts, unknown = _state(db, actor)
    cells, unknown = advance(cells, now - state_ts, unknown)

    n = sum(cells.values())
    total = n + unknown + PRIOR_TOTAL
    if total <= 0:
        return {'cells': {}, 'unknown': 0.0, 'n': 0.0, 'clarity': 0.0,
                'top': None, 'prior': 0.0, 'arousal': 0.0}

    prior = PRIOR_TOTAL / BUCKETS
    full = {(a, i): prior for a in ANGLES for i in INTENSITIES}
    for k, v in cells.items():
        full[k] = full.get(k, prior) + v
    probs = {k: v / total for k, v in full.items()}
    p_unknown = (unknown + prior) / total

    ps = list(probs.values()) + [p_unknown]
    h = -sum(p * math.log(p) for p in ps if p > 0)
    clarity = max(0.0, 1.0 - h / math.log(len(ps))) if len(ps) > 1 else 0.0
    top = max(cells, key=lambda k: cells[k]) if cells else None
    return {'cells': probs, 'unknown': p_unknown, 'n': n,
            'clarity': clarity, 'top': top,
            # 归一化后的先验：一个「没有任何证据」的格子应该是多少。
            # 测试和调用方拿它当基准，比反推 PRIOR_TOTAL/total 可靠。
            'prior': prior / total,
            'arousal': arousal(cells)}


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
