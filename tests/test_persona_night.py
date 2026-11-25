"""可见域、夜里、人格版本。三块，各测自己的失败方式。

  scope    漏了 → 私有记忆从别的实例那里冒出来（我们刚发现 scope 写了三个月没人读）
  night    判早了 → 他在的时候人格被换掉，第二天才发现
  persona  管松了 → 她每晚都变，变成一个自我修饰永动机
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import night as N
from gits import persona, project
from gits.db import DB, Writer
from gits.llm import Reply
from gits.recall import recall, reindex
from gits.scope import self_of, visible

T0 = 1_800_000_000_000
MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _node(db: DB, nid: str, scope: str, text: str) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)", (nid, "episode", T0, scope, 1.0, 1.0, T0))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, text, T0))
    reindex(db)


# ── scope ────────────────────────────────────────────────────


def test_看不见别人的私事(db) -> None:
    _node(db, "n:a", self_of("her"), "她私下想过要离开")
    _node(db, "n:b", self_of("peer1"), "peer1 私下想过要离开")
    _node(db, "n:c", "world", "公开说过要离开")
    got = {r.node_id for r in recall(db, "离开", actor="her", now_ms=T0)}
    assert "n:a" in got and "n:c" in got
    assert "n:b" not in got, "别人心里的算盘不该进她的检索"


def test_actor_没有默认值(db) -> None:
    with pytest.raises(TypeError):
        recall(db, "离开", now_ms=T0)          # type: ignore[call-arg]


def test_私有域按_actor_分(db) -> None:
    assert self_of("her") not in visible("peer1")
    assert self_of("peer1") == "self:peer1"


def test_事件也分可见域(db) -> None:
    Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                            ts=T0, recv_ts=T0, payload="她的".encode(),
                            scope=self_of("her"))
    Writer(db).append_event(source="shell", kind="msg_in", ext_id="m2",
                            ts=T0, recv_ts=T0, payload="别人的".encode(),
                            scope=self_of("peer1"))
    from gits.respond import recent

    assert [t for _w, t in recent(db, actor="her")] == ["她的"]


# ── night ────────────────────────────────────────────────────


def _day_pattern(db: DB, *, days: int = 21) -> None:
    """白天每两小时说一句，晚上 0–7 点一句话都没有。"""
    t = T0 - days * DAY
    while t < T0:
        hour = ((t // HOUR) % 24)
        if 8 <= hour <= 22:
            Writer(db).append_event(source="shell", kind="msg_in",
                                    ext_id=f"m{t}", ts=t, recv_ts=t, payload=b"hi")
        t += 2 * HOUR


def test_夜里认得出(db) -> None:
    _day_pattern(db)
    quiet = T0 - (T0 % DAY) + 3 * HOUR          # 凌晨 3 点
    got = N.check(db, quiet)
    assert got.ok, got.why


def test_白天不认成夜里(db) -> None:
    _day_pattern(db)
    noon = T0 - (T0 % DAY) + 14 * HOUR
    assert not N.check(db, noon).ok


def test_样本不够就说不知道(db) -> None:
    for i in range(5):
        Writer(db).append_event(source="shell", kind="msg_in",
                                ext_id=f"m{i}", ts=T0 + i, recv_ts=T0 + i, payload=b"hi")
    got = N.check(db, T0 + HOUR)
    assert not got.ok and "看不出" in got.why


# ── persona ──────────────────────────────────────────────────


SEED = "我不太会哄人。我记得住具体的事。"


def _chat(monkeypatch, payload: str):
    calls = []

    def fake(messages, **kw):
        calls.append(messages)
        return Reply(text=payload, model="stub",
                     usage={"completion_tokens": 30, "prompt_tokens": 500},
                     latency_ms=5)

    monkeypatch.setattr(persona, "chat", fake)
    return calls


def _evidence(db: DB, n: int = 5) -> list[int]:
    ids = []
    for i in range(n):
        ev, _ = Writer(db).append_event(source="shell", kind="msg_in",
                                        ext_id=f"e{i}", ts=T0 + i, recv_ts=T0 + i,
                                        payload=f"第{i}句".encode())
        ids.append(ev)
    return ids


def test_没有新经历就不调模型(db, monkeypatch) -> None:
    calls = _chat(monkeypatch, "{}")
    assert persona.update(db, fallback=SEED) is None
    assert calls == [], "从空气里写自我描述是种病"


def test_改一句要有依据(db, monkeypatch) -> None:
    ids = _evidence(db)
    _chat(monkeypatch, json.dumps({"keep": False, "changes": [
        {"old": "我记得住具体的事。", "new": "我记得住具体的事，但不追问。",
         "because": [ids[0], ids[1]]}]}, ensure_ascii=False))
    got = persona.update(db, fallback=SEED)
    assert got is not None and not got.keep and got.changes == 1
    assert "但不追问" in got.narrative
    assert persona.latest(db).version == 1


def test_没有依据的改动整次作废(db, monkeypatch) -> None:
    _evidence(db)
    _chat(monkeypatch, json.dumps({"changes": [
        {"old": "我记得住具体的事。", "new": "我什么都不记得。", "because": []}]},
        ensure_ascii=False))
    with pytest.raises(ValueError, match="没有依据"):
        persona.update(db, fallback=SEED)
    assert persona.latest(db) is None


def test_依据的事件必须真实存在(db, monkeypatch) -> None:
    _evidence(db)
    _chat(monkeypatch, json.dumps({"changes": [
        {"old": "我记得住具体的事。", "new": "我记不住了。",
         "because": [999999]}]}, ensure_ascii=False))
    with pytest.raises(ValueError, match="不存在"):
        persona.update(db, fallback=SEED)


def test_改太多就当它在重写(db, monkeypatch) -> None:
    ids = _evidence(db)
    _chat(monkeypatch, json.dumps({"changes": [
        {"old": "我记得住具体的事。", "new": "长" * 100,
         "because": [ids[0]]}]}, ensure_ascii=False))
    with pytest.raises(ValueError, match="改太多"):
        persona.update(db, fallback=SEED)


def test_没变化也是合法结果(db, monkeypatch) -> None:
    _evidence(db)
    calls = _chat(monkeypatch, json.dumps({"keep": True, "changes": []}))
    got = persona.update(db, fallback=SEED)
    assert got is not None and got.keep and got.changes == 0
    assert len(calls) == 1
    assert persona.latest(db).version == 0, "没变就不该涨版本号"


def test_人格版本能被重放(db, monkeypatch) -> None:
    ids = _evidence(db)
    _chat(monkeypatch, json.dumps({"changes": [
        {"old": "我记得住具体的事。", "new": "我记得住具体的事，但不追问。",
         "because": [ids[0]]}]}, ensure_ascii=False))
    persona.update(db, fallback=SEED)
    for r in db.q("SELECT id FROM events WHERE kind='msg_in'"):
        Writer(db).append_model_call(kind="extract", model="stub",
                                     event_id=r["id"], response='{"summary":"x"}')
    n = project.restore(db)
    assert n == 6, "五条消息 + 一版人格"
    got = db.scalar(
        "SELECT c.content FROM node_content c JOIN nodes n ON n.id=c.node_id"
        " WHERE n.kind='self'")
    assert "但不追问" in got
