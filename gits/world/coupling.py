"""自建预测：**从时空规律里学他**——不是有人告诉我们，是我们自己估的。

两种预测的区别不是实现，是**证据的来源**：

    world/forecast.py   外部预测   有人替我们看过。可能是错的，但可以核对。
    world/coupling.py   自建预测   我们自己的估计。永远带不确定性，不能说成事实。

所以这里的每一个函数返回的都是**分布或概率**，没有一个返回"他会怎样"。

## 耦合不是加一个维度，是加一个先验

直译"时空耦合"是学 s(t, p)：24 个时段 x 7 天 x 8 个场所 = 1344 格。
每天 30 条消息、一个月 900 条 -> 0.67 条/格。**大部分格子永远是空的。**

估出来的是噪声，而噪声的坏处不是"不准"，是**它会在错误的时刻很有把握地说话**。

所以用收缩（empirical Bayes）：

    全局 p0（该小时他在发消息的率）   <- 样本足够，当先验
    场所层 p(t|p)                     <- 以 p0 为先验的后验
    收缩强度                          <- 由该场所该小时的样本量决定

    p = (k + m0 * p0) / (n + m0)

数据少的格自动退化成全局；数据多的格才长出个性。**没有一个是配置出来的。**

## 分母内生：按时间加权，不按条数

手机上报是**位置变了才触发**，不是定时轮询。于是"一条定位"不等于"一个时刻"：

    他在家待一整天   -> 一条定位代表 8 小时
    他通勤来回走     -> 一条定位代表 2 分钟

按每条定位算率，等于在移动时段过采样、在久坐时段欠采样——而**久坐的地方
恰恰是家**。也就是说，采样过程和被估的量是相关的，这不是精度问题，是偏了。

所以每条定位带一个权重：它代表的那段时长（到下一次定位为止，封顶 MAX_GAP）。
率 = 加权和 / 权重和。这和无偏估计是同一件事：样本不均匀就按时长加权。
和 association=(w+a)/(w+a+b) 是同一个模式，闭式解，不需要 MCMC。
"""

from __future__ import annotations

from typing import Any

from gits.core.db import DB

#: 先验的等效样本量。20 的意思是"至少 20 次观测才让场所层压过全局"。
PRIOR_N = 20.0
#: 一条消息算在"紧接着"的窗口内
WINDOW_MS = 5 * 60_000
#: 转移的拉普拉斯平滑：宁可猜"待着不动"
STAY_PRIOR = 2.0


def _fixes(db: DB, *, limit: int = 20000) -> list[tuple[int, int]]:
    """[(ts, place_id)] 按时间升序。"""
    return [(int(r["ts"]), int(r["place_id"])) for r in db.q(
        "SELECT ts, place_id FROM place_fixes WHERE ts>0 ORDER BY ts LIMIT ?",
        (limit,))]


def weights(db: DB, *, limit: int = 20000) -> list[float]:
    """每条定位代表的那段时长，**归一化成均值 1**。

    归一化是为了让有效样本量还叫"条数"，和 PRIOR_N 的量纲对得上。
    """
    rows = [(int(r["ts"]), int(r["dwell_ms"] or 0)) for r in db.q(
        "SELECT ts, dwell_ms FROM place_fixes WHERE ts>0 ORDER BY ts LIMIT ?",
        (limit,))]
    if not rows:
        return []
    gaps = [max(0, d) for _ts, d in rows]
    # 最后一条没有"下一条"，用中位数补，免得被 0 拖偏
    nz = sorted(g for g in gaps if g > 0)
    fill = nz[len(nz) // 2] if nz else 1
    gaps[-1] = gaps[-1] or fill
    mean = sum(gaps) / len(gaps) or 1.0
    return [g / mean for g in gaps]


def _msgs(db: DB, *, limit: int = 20000) -> list[int]:
    return [int(r["ts"]) for r in db.q(
        "SELECT ts FROM events WHERE kind='msg_in' ORDER BY ts LIMIT ?", (limit,))]


def _hour(ts: int) -> int:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts / 1000).hour


def _followed(mts: list[int], ts: int) -> bool:
    """这条定位之后 WINDOW_MS 内他说话了没。"""
    import bisect

    i = bisect.bisect_left(mts, ts)
    return i < len(mts) and (mts[i] - ts) <= WINDOW_MS


def rates(db: DB, *, now: int) -> dict[str, Any]:
    """{place_id: {hour: 率}} + 全局 {hour: 率}。**收缩过的。**"""
    fixes = _fixes(db)
    mts = _msgs(db)
    if not fixes:
        return {"global": [0.0] * 24, "by_place": {}, "n": 0}

    w = weights(db, limit=len(fixes))
    if len(w) != len(fixes):
        w = [1.0] * len(fixes)

    gk = [0.0] * 24
    gn = [0.0] * 24
    pk: dict[int, list[float]] = {}
    pn: dict[int, list[float]] = {}
    for (ts, pid), ww in zip(fixes, w):
        h = _hour(ts)
        hit = _followed(mts, ts)
        gn[h] += ww
        gk[h] += ww if hit else 0.0
        pk.setdefault(pid, [0.0] * 24)[h] += ww if hit else 0.0
        pn.setdefault(pid, [0.0] * 24)[h] += ww

    # 全局：Beta(k+1, n-k+1) 的后验均值。没有样本就是 0.5 的均匀先验。
    g = [(gk[h] + 1) / (gn[h] + 2) for h in range(24)]
    by_place = {}
    for pid, k in pk.items():
        n = pn[pid]
        by_place[pid] = [((k[h] + PRIOR_N * g[h]) / (n[h] + PRIOR_N))
                         for h in range(24)]
    return {"global": g, "by_place": by_place, "n": len(fixes)}


def arrival(db: DB, *, now: int, place_id: int | None = None,
            hour: int | None = None) -> float:
    """他这会儿（在这个场所）发消息的率。**是个估计，不是事实。**"""
    r = rates(db, now=now)
    h = _hour(now) if hour is None else hour % 24
    if place_id is not None and place_id in r["by_place"]:
        return float(r["by_place"][place_id][h])
    return float(r["global"][h])


def next_place(db: DB, *, now: int) -> list[tuple[int, float]]:
    """接下来他会去哪：[(place_id, 概率)]，按当前场所条件。

    **向"待着不动"收缩**——没有证据时最合理的猜测是不动。
    """
    fixes = _fixes(db)
    if not fixes:
        return []
    cur = fixes[-1][1]
    counts: dict[int, float] = {}
    total = 0
    for (_t0, a), (_t1, b) in zip(fixes, fixes[1:]):
        if a != cur:
            continue
        counts[b] = counts.get(b, 0.0) + 1.0
        total += 1
    out = []
    for pid in set(counts) | {cur}:
        c = counts.get(pid, 0.0)
        prior = STAY_PRIOR if pid == cur else 0.0
        out.append((pid, (c + prior) / (total + STAY_PRIOR)))
    out.sort(key=lambda x: -x[1])
    return [(pid, round(p, 6)) for pid, p in out]


def outlook(db: DB, *, now: int) -> dict[str, Any]:
    """一格给未来的自己用的摘要。**现在没有调用方**——它是接口，不是装饰。"""
    from gits.world import places

    p = places.current(db)
    pid = int(p["id"]) if p else None
    return {
        "place": pid,
        "arrival": round(arrival(db, now=now, place_id=pid), 4),
        "next": next_place(db, now=now)[:3],
    }
