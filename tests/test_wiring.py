"""这一轮的四处修复：情绪归属、注意力降权、闲时准入、归因。

它们都不是"崩了"，是**算出来了但用错/没用**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.io import shell as SH
from gits.memory import extract as E
from gits.memory.recall import reindex
from gits.social import affect, state as slow
from gits.speech import ctxwin

T0 = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


class Chan:
    name = "weixin"
    names = frozenset({"weixin"})

    def __init__(self, items):
        self.items = items

    def poll(self):
        return self.items

    def ack(self):
        pass

    def deliver(self, row):
        return "sent", "m1"


# ── ① 情绪归属 ──────────────────────────────────────────────


def test_他说的情绪不会被记成她的(db) -> None:
    """抽取返回的是**说话者**的情绪；微信入站是 source=shell。

    以前一律归 her，于是微信里他每句话都成了她的心情。
    """
    sh = SH.Shell(db=db, channels=[Chan([{"text": "开会开到吐", "ext_id": "m1",
                                        "from": "him-openid"}])])
    sh.intake(t=T0)
    ev = db.one("SELECT * FROM events WHERE kind='msg_in'")
    assert E._speaker(db, dict(ev)) == "him-openid"
    assert E._speaker(db, dict(ev)) != "her"


def test_她说的话归她自己(db) -> None:
    assert E._speaker(db, {"kind": "msg_out", "source": "weixin"}) == "her"
    assert E._speaker(db, {"kind": "inner", "source": "ghost"}) == "her"


def test_peer_说的归那个_peer(db) -> None:
    assert E._speaker(db, {"kind": "msg_in", "source": "peer:peer1"}) == "peer1"


def test_她的心情由慢变量推出来(db) -> None:
    """**情绪由算法给出，不由模型读文本判断。**"""
    slow.observe_event(db, "her", "reply_fast", now=T0)
    d = affect.distribution(db, "her", now=T0)
    assert d["n"] > 0, "行为事件该推动她的情绪"
    assert d["top"][0] in ("joy", "trust")


def test_小波动不动情绪(db) -> None:
    slow.observe_event(db, "her", "no_reply", now=T0)
    d = affect.distribution(db, "her", now=T0)
    assert d["n"] < 1.0, "被忽略一次不该立刻改心情"


# ── ② 注意力降权 ────────────────────────────────────────────


def test_低于一的权重能降权(db) -> None:
    """早先是 max(1.0, w)，于是低于 1 的偏好永远降不了权。

    构造：咖啡那条**基础分更高**（稳定期长 → r 高），
    只靠 0.1 的权重把它压下去——如果 max(1.0, w)，它压不下去。
    """
    from gits.memory.recall import recall

    # n:a 很旧（r 低），n:b 很新（r 高）——基础分 b 高
    for nid, text, touched in (("n:a", "开会", T0 - 300 * DAY),
                               ("n:b", "咖啡", T0)):
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r,"
                      " last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "episode", touched, "world", 1.0, 1.0, touched))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (nid, text, touched))
    reindex(db)

    from gits.social.attention import seed as attention_seed

    got = [r.node_id for r in recall(db, "开会 咖啡", actor="her", now_ms=T0)]
    assert got[0] == "n:b", "没有权重时，基础分高的排前面（前提）"

    attention_seed(db, "her", {"咖啡": 0.1})
    got = [r.node_id for r in recall(db, "开会 咖啡", actor="her", now_ms=T0)]
    assert got[0] == "n:a", "0.1 的权重该把它压下去——max(1.0, w) 压不下去"


# ── ③ 闲时准入 ──────────────────────────────────────────────


def test_空闲预测不可信就不整理(db) -> None:
    from gits.time.idle import Idle

    wid = ctxwin.open_window(db, ts=T0)
    with Writer(db).txn() as c:
        c.execute("UPDATE ctx_windows SET tokens=?, turns=? WHERE id=?",
                  (int(ctxwin.BUDGET_TOKENS * 0.65), 4, wid))
    shaky = Idle(p10_ms=600_000, p50_ms=900_000, p90_ms=1_200_000,
                 confidence=0.075, basis="只有 3 条间隔")
    plan = ctxwin.plan(db, now=T0, idle=shaky, compact_ms=20_000)
    assert plan.action == "append"
    assert "可信" in plan.reason


def test_整理耗时有历史就照历史算(db) -> None:
    assert ctxwin.compact_ms_from_history(db) == ctxwin.DEFAULT_COMPACT_MS
    for i, ms in enumerate((8000, 12000, 30000)):
        Writer(db).append_event(
            source="ghost", kind="consolidate", ext_id=f"c{i}",
            ts=T0 + i, recv_ts=T0 + i,
            payload=json.dumps({"summary": "x", "latency_ms": ms}).encode())
    assert ctxwin.compact_ms_from_history(db) == 12000, "取中位数"


# ── ④ 归因 ──────────────────────────────────────────────────


def test_接话记下了是在回哪一句(db) -> None:
    """`ref_event_id` 以前是 NULL——事后分不清她回的是谁。"""
    # 顺序和真实情况一致：先发出去（outbox=sent），他再回
    with Writer(db).txn() as c:
        c.execute("INSERT INTO outbox(ts, channel, text, status)"
                  " VALUES(?,?,?,'sent')", (T0, "weixin", "在"))
    sh = SH.Shell(db=db, channels=[Chan([{"text": "在吗", "ext_id": "m1",
                                        "from": "him"}])])
    sh.intake(t=T0 + HOUR)          # 走真实入口，不直接调私有方法
    ev = db.one("SELECT id FROM events WHERE kind='msg_in'")
    row = db.one("SELECT ref_event_id FROM outcomes WHERE kind='reply'")
    assert row["ref_event_id"] == int(ev["id"]), \
        f"记的是 {row['ref_event_id']}，应该记那条消息的事件 id"
