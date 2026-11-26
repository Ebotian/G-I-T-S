"""说话的样子：**算出来的，不是写死的。**

以前 PersonaSeed 里有四条固定规则（短句优先 / 不用列表 / 不确定就说不知道）。
那是硬编码：她对谁、什么心情、多久没见，说的话一模一样。

现在这些从三个量里算：

    cadence     最近多频繁地交流 → 常联系的节奏下，句子自然更短更随意
    confidence  底气             → 底气低的时候话少、留余地
    affect      此刻的情绪分布   → 激动时短，低落时更短

**不硬编码，允许变动，由语义/统计计算给出。**

落点在 **B 段**（一小时一换），不是 A 段——A 段一天一变，塞进会变的风格
等于每轮都把缓存前缀打碎（50 倍价差）。
"""

from __future__ import annotations

from gits.core.db import DB
from gits.social import state as slow

#: 节奏高出基线这么多倍，才算"最近很密"
DENSE = 1.6
#: 底气低于这个值算"没底"
LOW_CONFIDENCE = 0.35


def hints(db: DB, actor: str, *, now: int) -> tuple[str, ...]:
    """当前该怎么说。空元组 = 没什么特别要调的（默认样子）。"""
    from gits.social.affect import distribution

    st = slow.state(db, actor, now=now)
    cadence = st.get("cadence", 0.2)
    conf = st.get("confidence", 0.5)
    mood = distribution(db, actor, now=now)

    out: list[str] = []
    dense = cadence >= slow.CFG["cadence"]["baseline"] * DENSE
    quiet = cadence <= slow.CFG["cadence"]["baseline"] * 0.6

    # 节奏密 → 更短、更像对话；节奏稀 → 稍微完整一点，别显得敷衍
    if dense:
        out.append("最近你们说得密，句子再短一点。")
    elif quiet:
        out.append("最近说得少，别太碎，一句说完整。")

    # 底气低 → 留余地，不用绝对判断
    if conf < LOW_CONFIDENCE:
        out.append("你现在没什么底气，话别说满，用「可能」「好像」。")
    elif conf > 0.75:
        out.append("你现在挺有底气的，可以直接说。")

    # 情绪：激动时更短，低落时更短且不解释
    if mood["n"] >= 1.0 and mood["arousal"] > 0.6:
        out.append("你有点上头，别长篇大论。")
    elif mood["n"] >= 1.0 and mood["unknown"] < 0.3 and mood["clarity"] > 0.4:
        out.append("你情绪挺明确，不用绕。")

    return tuple(out)


def line(db: DB, actor: str, *, now: int) -> str:
    """B 段里那一行。**没有变数时返回空串**，不占位置。"""
    hs = hints(db, actor, now=now)
    return "\n".join(hs) if hs else ""
