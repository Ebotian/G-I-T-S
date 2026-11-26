"""可见域、夜里、人格版本。三块，各测自己的失败方式。

  scope    漏了 → 私有记忆从别的实例那里冒出来（我们刚发现 scope 写了三个月没人读）
  night    判早了 → 他在的时候人格被换掉，第二天才发现
  persona  管松了 → 她每晚都变，变成一个自我修饰永动机
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.time import night as N
from gits.memory import persona
from gits.core import project
from gits.core.db import DB, Writer
from gits.speech.llm import Reply
from gits.memory.recall import recall, reindex
from gits.core.scope import self_of, visible

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
    from gits.speech.respond import recent

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



# ── persona：从自我域的节点里选，不是读文件 ──────────────────


def _self_node(db: DB, nid: str, text: str, *, ts: int = T0, s: float = 30.0,
               depth: int = 0, actor: str = "her") -> None:
    from gits.core.scope import self_of

    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch,"
                  " depth) VALUES(?,?,?,?,?,?,?,?)",
                  (nid, "episode", ts, self_of(actor), s, 1.0, ts, depth))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, text, ts))


def _chat(monkeypatch, payload: str):
    calls = []

    def fake(messages, **kw):
        calls.append(messages)
        return Reply(text=payload, model="stub",
                     usage={"completion_tokens": 60, "prompt_tokens": 500},
                     latency_ms=4)

    monkeypatch.setattr(persona, "chat", fake)
    return calls


def test_种子是一条普通事件(db) -> None:
    ev = persona.seed(db, "我不太会哄人。", now=T0)
    row = db.one("SELECT kind, scope, payload FROM events WHERE id=?", (ev,))
    assert row["kind"] == "seed" and row["scope"] == "self:her"
    assert row["payload"].decode() == "我不太会哄人。"


def test_没有自我描述时不说话(db) -> None:
    from gits.speech.respond import NoPersona, load_persona

    with pytest.raises(NoPersona, match="种子"):
        load_persona(db)


def test_初始设定和记忆地位平等(db) -> None:
    _self_node(db, "n:seed", "我不太会哄人。", s=30.0)
    _self_node(db, "n:recent", "今天他说了很多。", s=30.0)
    got = persona.candidates(db, now=T0)
    assert {c["id"] for c in got} == {"n:seed", "n:recent"}
    # 没有"这是人格所以要保护"的分支——排序只看 r 和深度
    assert [c["r"] for c in got] == pytest.approx([1.0, 1.0])


def test_想不起来的就不进人格(db) -> None:
    _self_node(db, "n:old", "很久以前的一句", ts=T0 - 400 * DAY, s=3.0)
    _self_node(db, "n:new", "最近的一句", ts=T0)
    got = [c["id"] for c in persona.candidates(db, now=T0)]
    assert got == ["n:new"], "初始设定如果一直没人提，就该掉出去"


def test_快照只用选中的节点(db, monkeypatch) -> None:
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _self_node(db, "n:b", "他说过想换个城市")
    calls = _chat(monkeypatch, "我不太会哄人。我记得住他讨厌什么。")
    got = persona.snapshot(db, now=T0)
    assert got is not None and len(got.picked) == 2
    assert "他讨厌没有结论的会" in calls[0][0]["content"]
    assert "我不太会哄人" in got.narrative


def test_快照是事件且能重放(db, monkeypatch) -> None:
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "我不太会哄人。")
    got = persona.snapshot(db, now=T0)
    assert got is not None
    ev = db.one("SELECT kind, payload FROM events WHERE id=?", (got.event_id,))
    assert ev["kind"] == "persona"
    import json as _json

    assert _json.loads(ev["payload"])["picked"] == ["n:a"]
    assert project.restore(db) >= 1


def test_没有可选节点就不调模型(db, monkeypatch) -> None:
    calls = _chat(monkeypatch, "x")
    assert persona.snapshot(db, now=T0) is None
    assert calls == [], "从空气里写自我描述是种病"


def test_上一版会进候选_自己引用自己(db, monkeypatch) -> None:
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "第一版。")
    persona.snapshot(db, now=T0)
    _self_node(db, "n:b", "他说想换个城市")      # 世界变了，才重写
    calls = _chat(monkeypatch, "第二版。")
    got = persona.snapshot(db, now=T0 + DAY)
    assert got is not None
    assert "第一版" in calls[0][0]["content"], "昨晚那版该在今天的输入里"


def test_输入没变就不重写(db, monkeypatch) -> None:
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "第一版。")
    assert persona.snapshot(db, now=T0) is not None
    calls = _chat(monkeypatch, "第二版。")
    assert persona.snapshot(db, now=T0 + DAY) is None, \
        "候选一模一样，重写只会是同一段意思的另一种说法——那是 churn"
    assert calls == []


def test_候选变了就重写(db, monkeypatch) -> None:
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "第一版。")
    persona.snapshot(db, now=T0)
    _self_node(db, "n:b", "他说想换个城市")
    _chat(monkeypatch, "第二版。")
    got = persona.snapshot(db, now=T0 + DAY)
    # 两个节点 + 上一版叙事（自反：昨晚那版今天还在输入里）
    assert got is not None and len(got.picked) == 3
    assert any("persona" in p for p in got.picked)


def test_候选没变就不会重写第二次(db, monkeypatch) -> None:
    """以前拿 picked 比——它还包含临时追加的上一版叙事，永远不相等。"""
    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "第一版。")
    assert persona.snapshot(db, now=T0) is not None
    calls = _chat(monkeypatch, "第二版。")
    assert persona.snapshot(db, now=T0 + DAY) is None
    assert calls == [], "输入一模一样，不该再调一次模型"


def test_inputs_和_picked_分开记(db, monkeypatch) -> None:
    import json as _j

    _self_node(db, "n:a", "他讨厌没有结论的会")
    _chat(monkeypatch, "第一版。")
    persona.snapshot(db, now=T0)
    # 第二次要真的发生，才谈得上「上一版进了 picked」
    _self_node(db, "n:b", "他说想换个城市")
    _chat(monkeypatch, "第二版。")
    persona.snapshot(db, now=T0 + DAY)
    ev = db.one("SELECT payload FROM events WHERE kind='persona'"
                " ORDER BY id DESC LIMIT 1")
    obj = _j.loads(ev["payload"])
    assert obj["inputs"] == ["n:a", "n:b"], "纯候选"
    assert any("persona" in p for p in obj["picked"]), "picked 里有上一版"


def test_历史人格快照不算新证据(db, monkeypatch) -> None:
    """重放会把旧人格快照投影成候选；把它算进输入的话，
    每写一版就多一份「支持自己」的证据，自我强化没有上限。
    """
    from gits.core import project as P

    # 种子必须走事件（直接写 nodes 的话，重放会把它抹掉）
    persona.seed(db, "他讨厌没有结论的会。", now=T0)
    P.restore(db)                       # 种子要先投影成节点，才能进候选
    _chat(monkeypatch, "第一版。")
    assert persona.snapshot(db, now=T0) is not None
    P.restore(db)                       # 夜里干的事

    cands = persona.candidates(db, now=T0 + DAY)
    kinds = {db.scalar("SELECT kind FROM nodes WHERE id=?", (c["id"],))
             for c in cands}
    assert "persona" not in kinds, f"快照混进了候选：{kinds}"
    assert len(cands) == 1 and cands[0]["content"].startswith("他讨厌"), \
        "候选只该有真实经历，不该有历史快照"
