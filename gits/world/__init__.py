"""外面的世界：**唯一知道「世界里有什么」的地方。**

上下文装配不认识位置、天气、场所——它只问这一层要两样东西：

    brief_text()       简要，每轮追加到上下文末尾（可核对的常识）
    catalog()/detail() 分类的详细，模型按需取

这条边界是刻意的：**装配只管拼，背景只管给。** 谁都能加字段的话，
上下文里迟早会长出没人知道从哪来的东西。

对照：gits.time 只管时间，gits.memory 只管记忆。这里只管世界。
"""

from __future__ import annotations

from gits.core.db import DB

__all__ = ["brief_text", "catalog", "detail"]


def brief_text(db: DB, *, now: int, actor: str = "her",
               head: bool = False) -> str:
    """每轮追加的那一段：一行 JSON。空 = 什么都不知道。

    head=True 时**额外带上拼接规则**——只在新开上下文时用一次。
    之后每轮只追加数据，不重复规则：规则重复一遍就是白烧 tokens，
    而且会让模型以为这是新一轮的指令。
    """
    import gits.world.brief as _bm

    body = _bm.render(_bm.build(db, now=now, actor=actor))
    if not body:
        return ""
    if head:
        return chr(10).join(_bm.RULES) + chr(10) + body
    return body


def catalog(db: DB, *, now: int, proactive: bool = False) -> tuple[dict, ...]:
    """分类目录：[(名字, 作用)]。**高频对话时返回空**——这是策略生效的地方。"""
    from gits.speech import pacing
    from gits.world import tools as _t

    ok, _why = pacing.tools_allowed(db, now=now, proactive=proactive)
    if not ok:
        return ()
    return tuple({"name": t["name"], "about": t["about"]} for t in _t.CATALOG)


def detail(db: DB, kind: str, *, now: int, proactive: bool = False,
           **kw) -> dict:
    """某一类的详细。**高频对话时一律空**——纵深防御，不指望调用方自觉。"""
    from gits.speech import pacing
    from gits.world import tools as _t

    ok, _why = pacing.tools_allowed(db, now=now, proactive=proactive)
    if not ok:
        return {}
    return _t.detail(db, kind, now=now, **kw)
