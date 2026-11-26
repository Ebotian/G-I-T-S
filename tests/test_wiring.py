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
from gits.speech import ctxwin, unresolved

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


# ── ⑥ 采样：不必有"可判定的理由"，但要有分布 ────────────────


def _dangling(db: DB, name: str, times: int = 3, gap_days: int = 1) -> None:
    from gits.core.project import entity_id
    from gits.memory.graph import add_edge

    for i in range(times):
        ts = T0 + i * gap_days * DAY
        nid = f"n:{name}-{i}"
        eid = entity_id(name)
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r,"
                      " last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "episode", ts, "world", 1.0, 1.0, ts))
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)


def test_抽样在候选之间有分布(db) -> None:
    """取 argmax 的话，永远只有同一件；抽样才能让别的事也有机会。"""
    _dangling(db, "看病")
    _dangling(db, "咖啡")
    picks = {unresolved.sample(db, now=T0 + m * 60_000).entity
             for m in range(40)}
    assert len(picks) == 2, f"40 个不同的分钟只抽到 {len(picks)} 件"


def test_同一分钟抽两次得到同一件(db) -> None:
    """一次决策要可复现——日志和重放才能对得上。"""
    _dangling(db, "看病")
    _dangling(db, "咖啡")
    a = unresolved.sample(db, now=T0)
    b = unresolved.sample(db, now=T0 + 1_000)      # 同一分钟
    assert a.entity == b.entity


def test_没有候选时返回空(db) -> None:
    assert unresolved.sample(db, now=T0) is None


def test_共鸣绑在同一条候选上(db) -> None:
    """以前共鸣是独立对人类最新消息算的，于是"最悬的是 A、最像的是 B"，
    两个分数相加却没有此刻说 A 的依据。现在先抽一件，再看它像不像。"""
    _dangling(db, "看病")
    _dangling(db, "咖啡")
    with Writer(db).txn() as c:
        c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                  " payload, lag, ts_trust, scope)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (T0, T0, "shell", "msg_in", "m1", "今天又喝了很多咖啡".encode(),
                   0, 1, "world"))
    i, _p, best = unresolved.inputs(db, now=T0)
    assert best is not None
    # 抽到咖啡时，共鸣才该是高的；抽到看病时不该被咖啡拉高
    if unresolved.name_of(db, best.entity) == "咖啡":
        assert i.resonance > 0, "抽到咖啡却没算出共鸣"


def test_回复落库前一定收成整句(db) -> None:
    """模型被 max_tokens 掐断时，落进 outbox 的必须是完整的句子。"""
    import json as _j

    from gits.core import project
    from gits.memory.persona import seed
    from gits.speech import respond as R
    from gits.speech.llm import Reply

    seed(db, "我不太会哄人。", now=T0)
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="在吗".encode())
    Writer(db).append_model_call(
        kind="extract", model="stub", event_id=ev,
        response=_j.dumps({"summary": "他打了招呼", "entities": [],
                           "claims": []}, ensure_ascii=False))
    project.restore(db)

    orig = R.chat
    R.chat = lambda *a, **k: Reply(
        text="我查了一下，蚂蚁靠触角认路，还有", model="stub",
        usage={"prompt_tokens": 100}, latency_ms=1)
    try:
        R.respond(db, R.pending(db)[0], now=T0)
    finally:
        R.chat = orig

    text = db.scalar("SELECT text FROM outbox ORDER BY id DESC LIMIT 1")
    assert "查了一下" not in text, f"交差腔漏出去了：{text}"
    assert not text.endswith(("，", "、")), f"半截话漏出去了：{text}"
