"""慢变量：交流节奏、底气。**慢的做快的基线。**

两个数，都按 OU 回归：

    cadence     最近多频繁地交流（每小时条数量级）。**有惯性**——
                频繁过之后再被冷落，不会立刻掉回基线，而是慢慢往下走。
    confidence  底气/自信度（Mehrabian 的 dominance，DESIGN §13 的 EE）。
                他接了话就涨一点，长时间没回应就慢慢落。

为什么要有惯性：人不会因为你三个小时没回就立刻回到陌生人的距离。
**保持上一个节奏**是有代价的（会显得黏人），但没有惯性更不像人——
它会让每一次沉默都被读成一次拒绝。

三个纪律：

  **事件驱动，不是文本驱动。**涨落来自可观测的行为（回得快慢、有没有回），
  不来自模型对一句话的情绪判断。后者是最不可靠的一环。
  **不硬编码语气/长度/文风。**它们由这些慢变量 + affect 算出来（见 speech/style.py）。
  **沉默是含糊的。**`no_reply` 的负面幅度必须远小于 `reply_fast` 的正面幅度。
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.core.db import DB, Writer
from gits.time.regulation import relax

#: 每个慢变量的 (基线, 时间常数小时, 上下界)
#: θ 越小越"迟钝"——惯性就来自这里
CFG = {
    "cadence":    {"baseline": 0.20, "tau_h": 36.0, "lo": 0.0, "hi": 1.0},
    "confidence": {"baseline": 0.50, "tau_h": 120.0, "lo": 0.0, "hi": 1.0},
}

#: 行为事件 → 各慢变量的增量。**没有一件事是模型判断出来的。**
#:
#: 两条纪律：
#:
#: ① **沉默是含糊的**：no_reply / long_silence 的负面幅度明显小于 reply_fast 的正面幅度。
#:
#: ② **自己说「我做了」不算数**（借自心潮·念的 proof 设计）。
#:    底气只由**外部确认**推动：他回了、回得快、说得长。
#:    `initiate`（主动开口这件事本身）只推节奏，不推底气——
#:    否则她越开口越有底气，而「我开口了」是我们自己的行为，不是世界给的证据。
#:    这和我们刚修掉的三条反事实问题是同一个病。
EVENT_DELTAS = {
    "reply_fast":   {"cadence": +0.06, "confidence": +0.03},
    "reply_slow":   {"cadence": +0.02, "confidence": +0.01},
    "long_reply":   {"cadence": +0.03, "confidence": +0.02},
    "initiate":     {"cadence": +0.04},
    "no_reply":     {"cadence": -0.01, "confidence": -0.005},
    "long_silence": {"cadence": -0.03, "confidence": -0.01},
}
#: 只有这些事件能推底气——写成表，别散在 if 里。
CONFIDENCE_EVENTS = frozenset(k for k, v in EVENT_DELTAS.items()
                              if "confidence" in v)


@dataclass(frozen=True)
class Slow:
    key: str
    value: float


def _cfg(key: str) -> dict:
    if key not in CFG:
        raise KeyError(f"未知的慢变量：{key}")
    return CFG[key]


def read(db: DB, actor: str, key: str, *, now: int) -> float:
    """当前值。**按 OU 回归推进到 now**——所以放久了自己会往回走。"""
    cfg = _cfg(key)
    row = db.one("SELECT value, ts FROM slow_state WHERE actor=? AND key=?",
                 (actor, key))
    if row is None:
        return float(cfg["baseline"])
    dt_h = max(0.0, (now - int(row["ts"])) / 3_600_000)
    return relax(float(row["value"]), float(cfg["baseline"]),
                 float(cfg["tau_h"]), dt_h)


def state(db: DB, actor: str, *, now: int) -> dict[str, float]:
    return {k: read(db, actor, k, now=now) for k in CFG}


def bump(db: DB, actor: str, key: str, delta: float, *, now: int) -> float:
    """推一把。先把当前值推进到 now，再加增量，然后夹在界内。"""
    cfg = _cfg(key)
    cur = read(db, actor, key, now=now)
    new = max(float(cfg["lo"]), min(float(cfg["hi"]), cur + delta))
    with Writer(db).txn() as c:
        c.execute(
            "INSERT INTO slow_state(actor, key, value, ts) VALUES(?,?,?,?)"
            " ON CONFLICT(actor, key) DO UPDATE SET value=?, ts=?",
            (actor, key, new, now, new, now))
    return new


def observe_event(db: DB, actor: str, event: str, *, now: int) -> dict[str, float]:
    """记一个行为事件，返回被推动的慢变量。""

    事件名和 DELTA 都是**声明式表格**，看得见、改得动、能被检查——
    不是散在代码里的 if/else。
    """
    deltas = EVENT_DELTAS.get(event)
    if not deltas:
        return {}
    out = {}
    for key, delta in deltas.items():
        out[key] = bump(db, actor, key, delta, now=now)
    _to_affect(db, actor, deltas, now=now)
    return out


#: 慢变量往哪个方向动 → 往哪一格情绪上推。
#: **这是「情绪由算法给出」的落点**：不由模型读文本判断，由状态变化推出来。
#: 方向不猜（confidence 掉 = 难受是常识），幅度按增量大小取 1 或 2 档。
STATE_TO_AFFECT = {
    ("confidence", +1): ("trust", 1),
    ("confidence", -1): ("sadness", 1),
    ("cadence", +1): ("joy", 1),
    ("cadence", -1): ("fear", 1),
}
#: 增量小于这个就不动情绪——日常小幅波动不该每次都改心情
AFFECT_EPS = 0.02


def _to_affect(db: DB, actor: str, deltas: dict[str, float], *,
               now: int) -> None:
    """把慢变量的变化折成一次情绪观测。"""
    from gits.social.affect import observe

    for key, delta in deltas.items():
        if abs(delta) < AFFECT_EPS:
            continue
        spec = STATE_TO_AFFECT.get((key, 1 if delta > 0 else -1))
        if spec is None:
            continue
        angle, _base = spec
        intensity = 2 if abs(delta) >= AFFECT_EPS * 3 else 1
        observe(db, actor, angle=angle, intensity=intensity, ts=now)


def regret_ratio(db: DB, actor: str, *, now: int) -> float:
    """节奏比基线高多少倍。>1 = 最近比平时密，<1 = 比平时稀。"""
    base = float(_cfg("cadence")["baseline"])
    return read(db, actor, "cadence", now=now) / max(1e-9, base)
