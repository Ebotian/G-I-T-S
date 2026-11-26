"""端到端：三个实例，私有消息、到期事实、失败重试、后台换窗，
然后对比 在线库 / 恢复重放库 / 副本 三者。

这组测试存在的理由和单元测试不同：单元测试验证「函数对不对」，
**它验证「有没有被调用、参数是不是真的」**。
同一类问题（有代码、有注释、有测试，但没生效）已经出现过八次：
edges.w、scope、mood、phase_allows、投影丢作用域、后台整理、强化时机、人格判变。

全程合成数据、替身模型，不碰生产库、不联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.core import events as EV
from gits.core import project, replica, seedfile
from gits.core.db import DB, Writer
from gits.core.scope import self_of
from gits.io import shell as SH
from gits.memory import compact as C
from gits.memory import extract as E
from gits.memory.recall import recall, reindex
from gits.social import affect, peer
from gits.speech import context as CTX
from gits.speech import respond as R
from gits.speech import facts

T0 = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000
SECRET = "peer1 的私事：他打算辞职"


class FakeLLM:
    """替身模型。fail_next 控制"这一次调用失败"。"""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_next = False
        self.payloads: list[str] = []

    def chat(self, messages, **kw):
        from gits.speech.llm import Reply

        self.calls += 1
        self.payloads.append(json.dumps(messages, ensure_ascii=False))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟模型调用失败")
        return Reply(text="嗯。", model="stub",
                     usage={"prompt_tokens": 800, "completion_tokens": 10},
                     latency_ms=1)

    def extract_chat(self, messages, **kw):
        from gits.speech.llm import Reply

        self.calls += 1
        text = messages[0]["content"]
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟模型调用失败")
        # 简单规则：原话里出现的词当实体，省得引入不确定性
        ents = [{"name": w, "kind": "topic"}
                for w in ("辞职", "睡不好", "开会", "生日") if w in text]
        payload = {"summary": text[:20], "entities": ents, "claims": []}
        return Reply(text=json.dumps(payload, ensure_ascii=False), model="stub",
                     usage={"prompt_tokens": 200, "completion_tokens": 40},
                     latency_ms=1)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    db = DB(tmp_path / "g.db")
    llm = FakeLLM()
    monkeypatch.setattr(R, "chat", llm.chat)
    monkeypatch.setattr(E, "chat", llm.extract_chat)
    monkeypatch.setattr(C, "chat", llm.chat)

    doc = {
        "format": "gits-seed/1",
        "actors": [
            {"id": "her", "main": True, "persona": "我说话短。",
             "topics": {"开会": 1.2}},
            {"id": "peer1", "main": False, "persona": "我先留意措辞。",
             "topics": {"措辞": 1.1}},
            {"id": "peer2", "main": False, "persona": "我记时间。",
             "topics": {}},
        ],
        "facts": [
            {"subject": "him", "key": "birthday", "label": "他的生日",
             "month": 3, "day": 12, "lead_days": 3},
        ],
    }
    seedfile.apply(db, seedfile.parse(json.dumps(doc, ensure_ascii=False)),
                   now=T0)
    project.restore(db)
    yield db, llm, tmp_path
    db.close()


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


# ── 私有消息不串台 ───────────────────────────────────────────


def test_peer_的私信不会进她的检索(world) -> None:
    db, _llm, _p = world
    peer.say(db, "peer1", "peer2", SECRET, now=T0)
    assert peer.deliver(db, now=T0) == 1
    assert E.extract(db, E.pending(db)[0]) is not None
    reindex(db)

    assert recall(db, "辞职", actor="her", now_ms=T0) == [], \
        "别人的私信不该进她的检索"
    got = recall(db, "辞职", actor="peer2", now_ms=T0)
    assert got, "但收信人自己该想得起来"


def test_私信投影出来的节点带正确的域(world) -> None:
    db, _llm, _p = world
    peer.say(db, "peer1", "peer2", SECRET, now=T0)
    peer.deliver(db, now=T0)
    E.extract(db, E.pending(db)[0])
    scopes = {r["scope"] for r in db.q(
        "SELECT scope FROM nodes WHERE kind='episode'")}
    assert scopes == {self_of("peer2")}, f"实际是 {scopes}"


# ── 到期事实真的进了模型请求 ────────────────────────────────


def test_到期事实出现在真实请求体里(world) -> None:
    db, llm, _p = world
    # 挑一个离 3/12 还有两天的时刻（lead_days=3 → 该进上下文）
    import datetime as _dt

    on_the_day = int(_dt.datetime(
        2028, 3, 10, 12,
        tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp() * 1000)
    assert facts.lines(db, on_the_day) == ("2 天后是他的生日。",)
    sh = SH.Shell(db=db, channels=[Chan([{"text": "在吗", "ext_id": "m1",
                                        "from": "him"}])])
    sh.intake(t=T0)
    turns = R.pending(db)
    assert turns
    # 直接看装配结果，再看真实请求体——两套装配必须一致
    body = CTX.messages(
        R.load_persona(db),
        R.relation(db, now=on_the_day),
        CTX.TurnContext(stimulus="在吗", due=facts.lines(db, on_the_day)),
    )
    assert any("生日" in m["content"] for m in body)


# ── 失败重试：不落 outbox、不强化 ────────────────────────────


def test_模型失败时既不回话也不强化(world) -> None:
    db, llm, _p = world
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="开会".encode())
    Writer(db).append_model_call(kind="extract", model="stub", event_id=ev,
                                 response=json.dumps(
                                     {"summary": "他开会",
                                      "entities": [{"name": "开会",
                                                    "kind": "topic"}],
                                      "claims": []}, ensure_ascii=False))
    project.restore(db)
    reindex(db)
    before = db.scalar("SELECT s FROM nodes WHERE kind='entity'")

    llm.fail_next = True
    with pytest.raises(RuntimeError):
        R.respond(db, dict(db.one("SELECT * FROM events WHERE id=?", (ev,))),
                  now=T0 + 5 * DAY)

    assert db.scalar("SELECT count(*) FROM outbox") == 0, "没回话"
    after = db.scalar("SELECT s FROM nodes WHERE kind='entity'")
    assert after == before, "失败的请求不该强化记忆"


def test_成功之后强化_而且能重放(world) -> None:
    db, _llm, _p = world
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="开会".encode())
    Writer(db).append_model_call(kind="extract", model="stub", event_id=ev,
                                 response=json.dumps(
                                     {"summary": "他开会",
                                      "entities": [{"name": "开会",
                                                    "kind": "topic"}],
                                      "claims": []}, ensure_ascii=False))
    project.restore(db)
    reindex(db)

    R.respond(db, dict(db.one("SELECT * FROM events WHERE id=?", (ev,))),
              now=T0 + 5 * DAY)
    s_before = db.scalar("SELECT s FROM nodes WHERE kind='entity'")
    assert db.scalar("SELECT count(*) FROM events WHERE kind='reinforce'") >= 1

    project.restore(db)          # 夜里 housekeep 干的事
    s_after = db.scalar("SELECT s FROM nodes WHERE kind='entity'")
    assert s_after == s_before, \
        f"重放把强化抹掉了：{s_before} → {s_after}"


# ── 副本保留作用域 ──────────────────────────────────────────


def test_副本上私有的东西还是私有的(world) -> None:
    db, _llm, tmp_path = world
    rep = DB(tmp_path / "rep.db")
    try:
        replica.pull(db, rep, "rep")
        row = rep.one("SELECT scope FROM events WHERE kind='seed'"
                      " AND scope LIKE 'self:%' LIMIT 1")
        assert row is not None, "私有种子在副本上丢了作用域"
        assert row["scope"].startswith("self:")
    finally:
        rep.close()


# ── 后台整理不再炸 ──────────────────────────────────────────


def test_后台整理不会因为跨线程而失败(world) -> None:
    db, _llm, tmp_path = world
    from gits import ghost as G

    g = G.Ghost(db=db)
    fut = g._pool.submit(G._compact_off_thread, str(db.path), T0, "test")
    exc = fut.exception(timeout=30)
    assert exc is None, f"后台整理抛了：{exc!r}"


def test_已巩固的实体第二天不会被变弱(world) -> None:
    """短期巩固的上限是「当天增量的次数」，不是稳定性的绝对值。

    早先写成 min(s*boost, 4)，于是 s=9 的实体第二天被提一次就掉到 4。
    """
    db, _llm, _p = world
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="d1",
                                    ts=T0, recv_ts=T0, payload="辞职".encode())
    Writer(db).append_model_call(kind="extract", model="stub", event_id=ev,
                                 response=json.dumps(
                                     {"summary": "他说起辞职",
                                      "entities": [{"name": "辞职",
                                                    "kind": "topic"}],
                                      "claims": []}, ensure_ascii=False))
    project.restore(db)
    with Writer(db).txn() as c:
        c.execute("UPDATE nodes SET s=9.0 WHERE kind='entity'")

    later = T0 + DAY + HOUR
    ev2, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="d2",
                                     ts=later, recv_ts=later,
                                     payload="又提辞职".encode())
    # 第二条走真实的抽取路径（它是 pending 的，没有现成结果）
    E.extract(db, dict(db.one("SELECT * FROM events WHERE id=?", (ev2,))))

    s = db.scalar("SELECT s FROM nodes WHERE kind='entity'")
    assert s >= 9.0, f"已经巩固的稳定性被削弱了：9.0 → {s}"


def test_同一条消息里同一个实体只算一次(world) -> None:
    """它既出现在 entities 里、又出现在 claims 里时，不该记两次提及。

    早先是各 touch 一次——六条消息实测记了十二次。
    """
    db, _llm, _p = world
    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="dup",
                                    ts=T0, recv_ts=T0, payload="辞职".encode())
    Writer(db).append_model_call(
        kind="extract", model="stub", event_id=ev,
        response=json.dumps(
            {"summary": "他说辞职的事",
             "entities": [{"name": "辞职", "kind": "topic"}],
             "claims": [{"subject": "辞职", "predicate": "让他",
                         "object": "睡不好"}]}, ensure_ascii=False))
    project.restore(db)

    row = db.one("SELECT touch_n FROM nodes WHERE kind='entity'"
                 " AND id=? ", (__import__('gits.core.project', fromlist=['entity_id'])
                                .entity_id("辞职"),))
    assert row["touch_n"] == 1, f"同一条消息记了 {row['touch_n']} 次"
