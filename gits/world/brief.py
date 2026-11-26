"""常驻背景块：**她此刻知道的，只有事实。**

它在上下文里占一行头 + 一行 JSON。三件事决定它的形状：

  ① **只放可核对的**
     场所、城市、待了多久、外面什么天、这个点的常态。
     预测（到达率、下一站）不进来——概率进了上下文就会变成话术
     （"你好像不太想说话"），而那不是从他的意思里来的。

  ② **每小时冻结**
     "通常不变"和"保证不变"是两件事。冻结之后一小时内逐字节相同，
     前缀才真的稳。冻结失败过一次：relation() 每轮重算，注释写着
     "一小时内逐字不变"，实际每小时都可能变。

  ③ **派生量不落库**
     待了多久 = now - since，渲染时算，不写任何表、不进任何学习。
     它在呈现上是事实，在统计上是**右删失**（这一趟还没结束），
     所以永远不进停留时长分布。
"""

from __future__ import annotations

import json
from typing import Any

from gits.core.db import DB

#: 待了多久的分桶。**不给分钟**——给了模型就会开始算钟点，
#: 而墙钟只做参照层。分桶保留了"待挺久了"这个信息，丢掉了钟面。
STAY_BUCKETS = ((30, "刚坐下"), (120, "待了一会儿"), (300, "待了挺久"),
                (720, "待了大半天"), (10 ** 9, "一直在这儿"))

#: 哪些字段变了才值得追加一条。其余（温度 ±1、湿度）不追加。
SUBSTANTIVE = ("loc", "usual", "weather")

#: 没有别名的占位词。**不编名字**——从「夜里 90% 在这」推断出「这是家」是猜，
#: 猜错了她会在你上班时问「到家了？」。给占位词，让模型自己决定要不要问。
PLACEHOLDER = "ASK_TO_REPLACE"

#: 只在新开上下文时出现一次的**拼接规则**。之后每轮只追加数据，不重复规则。
RULES = (
    "# 下面每轮末尾追加一行背景（JSON）。只信最后一行；"
    "那是你知道的事，不是要复述的话。",
    "# loc=ASK_TO_REPLACE 表示那个地方还没有名字——想知道就可以问。",
)


def _stay(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    for edge, word in STAY_BUCKETS:
        if minutes < edge:
            return word
    return STAY_BUCKETS[-1][1]


def build(db: DB, *, now: int, actor: str = "her") -> dict[str, Any]:
    """**纯函数，只读表。** 同一份数据 + 同一个小时 → 同一份结果。"""
    from gits.social import affect
    from gits.time import bin_of
    from gits.world import amap, loc, places

    out: dict[str, Any] = {}

    # **先看位置还新不新。** 场所状态是"上次确认在那里"，不是"现在还在那里"。
    # 没有新鲜读数时，两个字都不能说：loc 会变成"他现在在某处"的断言，
    # stay 会把"七天没上报"写成"连续待了七天"。
    fresh = loc.fresh(db, now=now)
    cur = places.current(db, actor=actor) if fresh else None
    if cur:
        since = int(cur.get("since") or 0)
        if since:
            out["stay"] = _stay((now - since) / 60_000.0)

    # **具体地点，不是城市。** 城市对聊天没有用——"你在温州"不构成任何话题；
    # "你在公司"才有。没有别名就给占位词，让模型知道这件事可以问。
    if cur:
        out["loc"] = places.name_of(cur, db) or PLACEHOLDER

    w = amap.latest(db, now=now)
    if w and not amap._stale(w, now=now, ttl=amap.FRESH_MS):
        weather = (w.get("weather") or "").strip()
        temp = w.get("temp")
        if weather:
            out["weather"] = weather
        if isinstance(temp, (int, float)):
            out["temp"] = int(round(temp))

    d = affect.distribution(db, actor, now=now)
    top = d.get("top")
    if top and d.get("n", 0) >= 1.0:
        angle, inten = top
        word = affect.ZH.get((angle, inten), angle)
        out["mood"] = word

    # 这个点的常态：profile 里样本最多的那个场所。**可核对的事实，不是预测。**
    hour = bin_of(now) % 24
    best, best_n = None, 0
    for p in places.real_places(db):
        hits = places.profile(db, int(p["id"]))[hour]
        if hits > best_n:
            best, best_n = p, hits
    if best is not None and best_n >= 3:
        name = places.name_of(best, db)
        if name and name != out.get("loc"):
            out["usual"] = name

    return out


def render(brief: dict[str, Any]) -> str:
    """确定性序列化：键排序、无空格、不带时间戳。

    **一个时间戳就能让后面全部失效**，所以这里不许出现任何"现在几点"。
    """
    if not brief:
        return ""
    return json.dumps(brief, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
