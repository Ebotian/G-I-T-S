"""回复闭环：装配 → 调模型 → 落 outbox。

不联网。模型调用用桩，验的是"接线对不对"：
  A 段在整轮里逐字不变（缓存纪律）
  每条 msg_in 只回一次（幂等）
  回复先落库再投递
  每次调用都记账，且能溯源到人格版本
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.speech import respond as R
from gits.core.db import DB, Writer
from gits.speech.llm import Reply

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    with Writer(d).txn() as c:
        c.execute("INSERT INTO contacts(actor_id, channel, last_context_token,"
                  " last_seen, last_text) VALUES('u1','weixin','ctx-1',?, '在吗')",
                  (T0,))
    yield d
    d.close()


def _persona(db: DB) -> None:
    from gits.memory.persona import seed
    seed(db, "我不太会哄人。我记得住具体的事。", now=T0)


def _msg(db: DB, text: str, ext: str = "m1") -> int:
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id=ext,
                                    ts=T0, recv_ts=T0, payload=text.encode())
    return ev


@pytest.fixture()
def stub(monkeypatch):
    calls = []

    def fake_chat(messages, **kw):
        calls.append(messages)
        return Reply(text="嗯。", model="stub", usage={"completion_tokens": 2},
                     latency_ms=1)

    monkeypatch.setattr(R, "chat", fake_chat)
    return calls


def test_一条消息只回一次(db, stub) -> None:
    _persona(db)
    ev = _msg(db, "开会开到吐")
    assert len(R.pending(db)) == 1
    R.respond(db, R.pending(db)[0])
    assert R.pending(db) == []
    assert len(stub) == 1


def test_回复先落库并带上收件人(db, stub) -> None:
    _persona(db)
    _msg(db, "在吗")
    t = R.respond(db, R.pending(db)[0])
    row = db.one("SELECT status, channel, meta, ref_event_id FROM outbox WHERE id=?",
                 (t.outbox_id,))
    assert row["status"] == "pending" and row["channel"] == "weixin"
    assert "u1" in row["meta"] and "ctx-1" in row["meta"]


def test_每次调用都记账且可溯源(db, stub) -> None:
    _persona(db)
    ev = _msg(db, "在吗")
    t = R.respond(db, R.pending(db)[0])
    call = db.one("SELECT kind, event_id, prompt_hash FROM model_calls")
    assert call["kind"] == "reply" and call["event_id"] == ev
    inf = db.one("SELECT node_id FROM influence WHERE outbox_id=?", (t.outbox_id,))
    assert inf["node_id"].startswith("persona:v")


def test_A_段在同一版人格下逐字不变(db, stub) -> None:
    _persona(db)
    _msg(db, "一", "m1")
    _msg(db, "二", "m2")
    for ev in R.pending(db):
        R.respond(db, ev)
    a1 = stub[0][0]["content"].split("# 我们现在")[0]
    a2 = stub[1][0]["content"].split("# 我们现在")[0]
    assert a1 == a2


def test_没有模型密钥时错误要能读懂(db, monkeypatch) -> None:
    from gits.speech.llm import LLMError, chat

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        chat([{"role": "user", "content": "hi"}])


def test_回复也从她的额度里记账(db, stub) -> None:
    """回复照记，但**不拦**——配额不该表现为她坏了。
    只记不拦这件事得有测试盯着：漏了记账，日上限就永远不会生效。"""
    from gits.io import ledger

    _persona(db)
    ev = _msg(db, "在吗")
    before = ledger.state(db, actor="her", now=T0)["spent_today"]
    R.respond(db, R.pending(db)[0], model="stub", now=T0)
    after = ledger.state(db, actor="her", now=T0)["spent_today"]
    assert after > before, "回复没有记账"
    raw = db.q("SELECT payload FROM events WHERE kind='spend'")
    assert any(b"reply" in bytes(r["payload"]) for r in raw)


def test_额度花光了也照样回复(db, stub) -> None:
    """**拦只拦主动开口。**他先说话了，她就得回。"""
    from gits.io import ledger

    _persona(db)
    s = ledger.state(db, actor="her", now=T0)
    ledger.charge(db, actor="her", kind="llm", cost=s["day_cap"], now=T0)
    assert ledger.state(db, actor="her", now=T0)["spendable"] == 0
    ev = _msg(db, "在吗")
    out = R.respond(db, R.pending(db)[0], model="stub", now=T0)
    assert out is not None and out.outbox_id
