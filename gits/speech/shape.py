"""前缀的稳定性：**测出来的，不是设计出来的。**

我们一直说"A + B 稳定、C 易变"，但那是**意图**。缓存只认字节：
前缀里只要有一个字符变了，后面全部重算。而 B 段里曾经放着
"最近在意谁（按 last_touch 排序）""最近偏什么情绪"——这两样每一轮都可能变，
于是 system 前缀每轮断一次，而**没有任何东西会报错**。

所以这里做一件事：每轮记下 A/B/C 三段的摘要，让"前缀稳不稳"变成可以查的数字。
（这个思路来自 pi-cache-optimizer 的 cache doctor：命中率低不只是调参问题，
先要能回答"是哪一段在动"。）

    变了的东西              算什么
    C 段每轮都变            正常——它本来就是"这一轮才有的"
    A 段变                  人格换版：一天一次可以接受
    B 段在同一个窗口里变     **bug**。它声称"一小时内逐字不变"
"""

from __future__ import annotations

import hashlib
import json

from gits.core.db import DB, Writer

#: 一天算一段。诊断只看最近这么久的形状。
WINDOW_MS = 86_400_000
#: 少于这么多轮不下结论——**三轮回合里命中率低，可能只是冷启动**。
MIN_TURNS = 4
#: 低于这个命中率才算异常
LOW_HIT = 0.30


def digest(text: str) -> str:
    """一段的指纹。8 位十六进制，够区分，够短。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def record(db: DB, *, now: int, a: str, b: str, c: str,
           window_id: int | None = None) -> int | None:
    """记下这一轮的形状。**只存指纹，不存正文**——正文是内容。"""
    body = json.dumps({"a": digest(a), "b": digest(b), "c": digest(c),
                       "w": window_id}, sort_keys=True)
    ev, _created = Writer(db).append_event(
        source="self", kind="ctx_shape", ext_id=f"ctx_shape:{now}",
        ts=now, recv_ts=now, payload=body.encode())
    return ev


def shapes(db: DB, *, now: int, window: int = WINDOW_MS) -> list[dict]:
    rows = db.q("SELECT ts, payload FROM events WHERE kind='ctx_shape'"
                " AND ts > ? ORDER BY ts", (now - window,))
    out = []
    for r in rows:
        try:
            obj = json.loads(r["payload"] or b"{}")
        except (json.JSONDecodeError, TypeError):
            continue
        obj["ts"] = int(r["ts"])
        out.append(obj)
    return out


def stats(db: DB, *, now: int, window: int = WINDOW_MS) -> dict:
    """命中率 + 三段各自变了几次。**这是"前缀稳不稳"的唯一事实来源。**"""
    from gits.speech import ctxwin

    rows = shapes(db, now=now, window=window)
    a_versions = len({r.get("a") for r in rows})
    b_versions = len({r.get("b") for r in rows})
    c_versions = len({r.get("c") for r in rows})

    # 同一个窗口里换过几次 B。**这才是真正要盯的数**：
    # 换窗口允许 B 变（新窗带新的头块），窗口之内不该变。
    per_window: dict[object, set] = {}
    for r in rows:
        per_window.setdefault(r.get("w"), set()).add(r.get("b"))
    b_per_window = max((len(v) for v in per_window.values()), default=0)

    hr = ctxwin.hit_rate(db)
    return {"turns": len(rows), "hit_rate": hr,
            "a_versions": a_versions, "b_versions": b_versions,
            "c_versions": c_versions, "b_per_window": b_per_window,
            "windows": len(per_window)}


def diagnose(db: DB, *, now: int, window: int = WINDOW_MS) -> list[str]:
    """命中率低的话，**是哪一段在动**。

    只在真有问题时出声（轮数够 + 命中低），或者结构上就不可能的事发生
    （同一个窗口里 B 变了）。别的时候闭嘴——一条常驻的警告等于没有警告。
    """
    s = stats(db, now=now, window=window)
    out: list[str] = []
    if s["turns"] < MIN_TURNS:
        return out

    # **结构性问题先报**：窗口之内 B 变了，前缀必然每轮断。
    if s["b_per_window"] > 1:
        out.append(f"⚠ B 段在同一个窗口里变了 {s['b_per_window']} 次"
                   "——system 前缀每轮都在断，缓存必然不命中")
        out.append("   找 B 段的输入：它们只允许依赖整点，不许依赖 now")

    hr = s["hit_rate"]
    if hr is None:
        return out
    if hr < LOW_HIT:
        out.append(f"📉 最近窗口命中率 {hr:.0%}（{s['turns']} 轮）")
        if s["a_versions"] > 1:
            out.append(f"   A 段变了 {s['a_versions']} 次（人格换版，可以接受）")
        if s["b_per_window"] == 1:
            out.append("   B 段是稳的——那就看 C 段之外还有什么在动"
                       "（工具目录、system 末尾、缓存过期）")
    return out


def line(db: DB, *, now: int, window: int = WINDOW_MS) -> str:
    """一行摘要。给 gits status 用。"""
    s = stats(db, now=now, window=window)
    hr = "?" if s["hit_rate"] is None else f"{s['hit_rate']:.0%}"
    return (f"缓存 命中={hr} 轮数={s['turns']} "
            f"A变{s['a_versions']} B变{s['b_versions']} C变{s['c_versions']}")
