"""两条「多余计算」的验收。

  ① 索引在写入时更新，不再每 15 分钟全量重建
  ② 回复只用一套装配（A 段哈希直接算，不再跑一遍 assemble）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.core import project
from gits.core.db import DB, Writer
from gits.memory.recall import recall

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _msg(db: DB, ext: str, text: str, summary: str) -> int:
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id=ext,
                                    ts=T0, recv_ts=T0, payload=text.encode())
    Writer(db).append_model_call(
        kind="extract", model="stub", event_id=ev,
        response=json.dumps({"summary": summary,
                             "entities": [{"name": "会议", "kind": "topic"}],
                             "claims": []}, ensure_ascii=False))
    return ev


def test_刚抽取的记忆立刻能检索(db) -> None:
    """**不经过重放**——走真实抽取路径，写完就该能检索。

    以前要等到下一次 15 分钟快照，而快照先清表再全量重插。
    注意查询词要和摘要共享二元组：「开会」匹配不到「开了很久的会」。
    """
    from gits.memory import extract as E
    from gits.speech.llm import Reply
    import gits.memory.extract as EX

    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="开会开到吐".encode())
    payload = {"summary": "他今天开会开很久",
               "entities": [{"name": "会议", "kind": "topic"}],
               "claims": []}
    orig = EX.chat
    EX.chat = lambda *a, **k: Reply(text=json.dumps(payload, ensure_ascii=False),
                                    model="stub", usage={}, latency_ms=1)
    try:
        E.extract(db, dict(db.one("SELECT * FROM events WHERE id=?", (ev,))))
    finally:
        EX.chat = orig

    hits = recall(db, "开会", actor="her", now_ms=T0)
    assert hits, "抽取写完就该能检索到，不该等快照"
    assert any("很久" in h.text for h in hits)
    assert recall(db, "会议", actor="her", now_ms=T0), "实体也要有索引"


def test_快照不再全量重建索引(db) -> None:
    """快照以前每 15 分钟 reindex 一次：先清表再遍历所有内容版本。

    它既慢，又让刚抽取的记忆要等到下一次快照才能检索。
    """
    import inspect

    from gits import ghost as G

    src = inspect.getsource(G.Ghost.snapshot)
    assert "reindex" not in src


def test_重放之后索引是干净的(db) -> None:
    """重放会清投影但不该留下孤儿索引。"""
    _msg(db, "m1", "开会", "他今天开了很久的会")
    project.restore(db)
    n1 = db.scalar("SELECT count(*) FROM node_fts")
    project.restore(db)
    n2 = db.scalar("SELECT count(*) FROM node_fts")
    assert n1 == n2, f"重放之后索引条数变了：{n1} → {n2}"


def test_同一内容不会重复建索引(db) -> None:
    _msg(db, "m1", "开会", "他今天开了很久的会")
    _msg(db, "m2", "又开会", "他又开会")
    project.restore(db)
    rows = db.q("SELECT node_id, count(*) n FROM node_fts GROUP BY node_id")
    assert all(r["n"] == 1 for r in rows), "一个节点只该有一条索引"


def test_回复只用一套装配(db, monkeypatch) -> None:
    """assemble() 在产出路径上只为了拿 A 段哈希——两套装配并存过，
    固定事实就是从它们的缝里漏掉的。"""
    from gits.social import affect
    from gits.speech import respond as R

    _msg(db, "m1", "在吗", "他问了在吗")
    project.restore(db)
    from gits.memory.persona import seed as pseed

    pseed(db, "我不太会哄人。", now=T0)
    project.restore(db)

    # 最强的不变量：产出路径上**根本没有** assemble 这个东西可调
    assert not hasattr(R, "assemble"), "两套装配又回来了"

    # 替身模型：这条测试关心的是装配次数，不是模型
    from gits.speech.llm import Reply

    monkeypatch.setattr(R, "chat", lambda *a, **k: Reply(
        text="嗯。", model="stub", usage={"prompt_tokens": 100}, latency_ms=1))

    calls = {"messages": 0}
    real_messages = R.messages

    def spy_messages(*a, **k):
        calls["messages"] += 1
        return real_messages(*a, **k)

    monkeypatch.setattr(R, "messages", spy_messages)
    R.respond(db, dict(db.one("SELECT * FROM events WHERE kind='msg_in'")),
              now=T0)
    assert calls["messages"] == 1, "整个回复只装配一次"
