"""上下文填充：按**频谱**分配，不是按分数取前 k 条。

这里的傅里叶不是对数据拟合，是对机制合成——
但基必须是**测出来的**，不能是发明的：

    一条记忆的特征频率 f = 1 / S      （S 是 FSRS 的稳定期，天）

于是：S 大的记忆是低频分量（多轮都不变，像 DC），
S 小的是高频分量（只对这一轮有效）。**频谱结构由遗忘曲线读出来，不是我们定的。**

它解决三个真问题：

  ① 现在按线性分数取前 k 条，完全可能返回「同一件事的 5 个版本」
     ——频谱视角看，那是同一个带被填满，别的带全空
  ② 漏斗压缩之后原件还在（当样本留），检索可能同时给出摘要和它的原件
     ——这叫不正交，必须去重
  ③ 固定的 k=5 没有依据；该按**频带的证据量**分预算

情绪的作用是**频谱倾斜**：激动抬高频（多讲具体那件事），平静抬低频（多讲一贯的样子）。
这是推出来的，不是规则写的——人激动时说细节，平静时说概括。
"""

from __future__ import annotations

import math

from gits.core.db import DB

#: 频带宽度：S 的 log2 刻度，以「1 天」为单位
BAND_UNIT_DAYS = 1.0
#: 倾斜强度。0 = 不分倾斜（纯按证据量分配），可关掉做对照
TILT = 0.6


def band_of(s_days: float | None) -> int:
    """稳定期 → 频带。S < 1 天都在 0 带（都是"这一轮的事"）。"""
    s = max(float(s_days or 0.0), BAND_UNIT_DAYS)
    return int(math.floor(math.log2(s / BAND_UNIT_DAYS)))


def _covered(db: DB, nid: str, candidates: set[str]) -> bool:
    """这条是不是已经被某个更深的候选覆盖了（被吸收过）。"""
    row = db.one("SELECT absorbed_into FROM node_content"
                 " WHERE node_id=? ORDER BY id DESC LIMIT 1", (nid,))
    if row is None or row["absorbed_into"] is None:
        return False
    return str(row["absorbed_into"]) in candidates


def allocate(db: DB, hits: list, *, slots: int = 5, arousal: float = 0.0,
             tilt: float = TILT) -> list:
    """把候选按频带分预算，选出进上下文的那几条。

    `hits` 是 `recall()` 的结果（Recalled，带 r 和 node_id）。
    返回同样类型，顺序为「先低频（概括）后高频（细节）」——
    写法上先给一贯的样子，再给这一件事。
    """
    if not hits or slots <= 0:
        return []
    cand_ids = {h.node_id for h in hits}
    # ① 正交：被更深候选吸收掉的不再单列（否则同一件事说两遍）
    kept = [h for h in hits if not _covered(db, h.node_id, cand_ids)]
    if not kept:
        return []

    # ② 分带：S 从库里读，不用 r 反推
    bands: dict[int, list] = {}
    for h in kept:
        s = db.scalar("SELECT s FROM nodes WHERE id=?", (h.node_id,))
        bands.setdefault(band_of(s), []).append(h)

    top_band = max(bands)
    weights: dict[int, float] = {}
    for b, rows in bands.items():
        mass = sum(max(0.0, float(h.r)) for h in rows)
        # ③ 频谱倾斜：激动抬**高频**（细节），平静抬**低频**（概括）。
        # 注意 level 的方向：0 带是最浅的（S<1 天），它才是高频。
        level = (1.0 - b / top_band * 2.0) if top_band > 0 else 1.0
        weights[b] = max(1e-9, mass * (1.0 + tilt * (arousal - 0.5) * level))

    total = sum(weights.values())
    if total <= 0:
        return []

    # ④ 预算：按份额分，但每个非空带先保底 1 个名额
    shares = {b: weights[b] / total for b in bands}
    entitlement = {b: slots * shares[b] for b in bands}
    quota = {b: max(1, int(entitlement[b])) for b in bands}
    while sum(quota.values()) > slots:
        trimmable = [b for b in quota if quota[b] > 1]
        if not trimmable:
            break
        b = min(trimmable, key=lambda x: shares[x])
        quota[b] -= 1
    while sum(quota.values()) < slots:
        b = max(quota, key=lambda x: entitlement[x] - quota[x])
        quota[b] += 1

    # ⑤ 带内按 r 取前几名；**低频在前**（先给一贯的样子，再给这一件事）
    out: list = []
    for b in sorted(quota, reverse=True):
        rows = sorted(bands[b], key=lambda h: -float(h.r))[:quota[b]]
        out.extend(rows)
    return out[:slots]


def arousal_of(cells: dict[tuple[str, int], float]) -> float:
    """从情绪分布里取激动程度（0..1）。和 affect.distribution 用的是同一套。"""
    total = sum(cells.values())
    if total <= 0:
        return 0.0
    return sum(v * (int(k[1]) - 1) / 2.0 for k, v in cells.items()) / total
