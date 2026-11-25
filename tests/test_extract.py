"""抽取：把一条消息变成图上的一小块。

不联网。模型用桩，验的是三条不能错的事：

  1. **先落 model_calls，再落投影**——反过来的话中途崩了，那条消息
     就永远抽不出来（重放会报 MissingModelResult）。
  2. **重放等价**：project.restore 只读 model_calls 里的文本，
     要和实时路径落出同样的图。
  3. **幂等**：同一条消息抽两遍，图不变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import extract as E
from gits import project
from gits.db import DB, Writer
from gits.llm import Reply

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _msg(db: DB, text: str, ext: str = "m1") -> dict:
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id=ext,
                                    ts=T0, recv_ts=T0, payload=text.encode())
    return dict(db.one("SELECT * FROM events WHERE id=?", (ev,)))


GOOD = json.dumps({"summary": "他今天开会开得很久",
                   "entities": [{"name": "会议", "kind": "topic"}],
                   "claims": [{"subject": "他", "predicate": "讨厌",
                               "object": "会议"}]}, ensure_ascii=False)


@pytest.fixture()
def stub(monkeypatch):
    monkeypatch.setattr(E, "chat", lambda *a, **k: Reply(
        text=GOOD, model="stub", usage={"completion_tokens": 50}, latency_ms=1))


def test_抽出节点和边(db, stub) -> None:
    ev = _msg(db, "开会开到吐")
    got = E.extract(db, ev)
    assert got.summary == "他今天开会开得很久"
    assert db.scalar("SELECT count(*) FROM nodes WHERE kind='episode'") == 1
    # entities 里的「会议」和 claims 里的「会议」是同一个实体——合并才对
    assert db.scalar("SELECT count(*) FROM nodes WHERE kind='entity'") == 2
    assert db.scalar("SELECT count(*) FROM edges") >= 2


def test_重放和实时落出同样的图(db, stub) -> None:
    ev = _msg(db, "开会开到吐")
    E.extract(db, ev)
    live = sorted((r["id"], r["kind"]) for r in db.q("SELECT id, kind FROM nodes"))
    content = sorted(r["content"] for r in db.q(
        "SELECT content FROM node_content WHERE node_id LIKE 'n:%'"))

    assert project.restore(db) == 1
    replay = sorted((r["id"], r["kind"]) for r in db.q("SELECT id, kind FROM nodes"))
    content2 = sorted(r["content"] for r in db.q(
        "SELECT content FROM node_content WHERE node_id LIKE 'n:%'"))
    assert replay == live
    assert content2 == content == ["他今天开会开得很久"]


def test_同一个实体不攒重复内容(db, stub) -> None:
    _msg(db, "开会开到吐", "m1")
    _msg(db, "又是会", "m2")
    for ev in E.pending(db):
        E.extract(db, ev)
    names = [r[0] for r in db.q(
        "SELECT content FROM node_content WHERE node_id LIKE 'e:%'")]
    assert sorted(names) == sorted(set(names)), "同名实体只该有一行内容"


def test_抽两遍图不变(db, stub) -> None:
    ev = _msg(db, "开会开到吐")
    E.extract(db, ev)
    n1 = db.scalar("SELECT count(*) FROM edges")
    E.apply(db, ev, E.parse(GOOD))          # 第二次只落投影
    assert db.scalar("SELECT count(*) FROM nodes WHERE kind='entity'") == 2
    assert db.scalar("SELECT count(*) FROM edges") == n1, "边是幂等的，不该翻倍"


def test_模型胡说不落账(db, monkeypatch) -> None:
    monkeypatch.setattr(E, "chat", lambda *a, **k: Reply(
        text="抱歉，我不明白", model="stub", usage={}, latency_ms=1))
    ev = _msg(db, "在吗")
    with pytest.raises(ValueError):
        E.extract(db, ev)
    # 原始输出留着——它才是"为什么抽不出来"的证据
    assert db.scalar("SELECT count(*) FROM model_calls WHERE kind='extract_failed'") == 1
    assert db.scalar("SELECT count(*) FROM model_calls WHERE kind='extract'") == 0
    assert db.scalar("SELECT count(*) FROM nodes WHERE kind='episode'") == 0
    # 失败不算"抽过了"，下一拍还会再试
    assert len(E.pending(db)) == 1


def test_抽不出来的不无限重试(db, monkeypatch) -> None:
    monkeypatch.setattr(E, "chat", lambda *a, **k: Reply(
        text="抱歉", model="stub", usage={}, latency_ms=1))
    ev = _msg(db, "在吗")
    for _ in range(E.MAX_TRIES):
        with pytest.raises(ValueError):
            E.extract(db, ev)
    assert E.pending(db) == [], "抽了 %d 次还不行，就放过它" % E.MAX_TRIES


def test_待抽的只看没抽过的(db, stub) -> None:
    _msg(db, "一", "m1")
    _msg(db, "二", "m2")
    assert len(E.pending(db)) == 2
    E.extract(db, E.pending(db)[0])
    assert [e["ext_id"] for e in E.pending(db)] == ["m2"]
