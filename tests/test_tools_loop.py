"""工具循环：一次、失败不影响回话、结果不进历史。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech import tools_loop
from gits.speech.llm import Reply

T0 = int(dt.datetime(2026, 11, 26, 12, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _reply(text: str, calls=None) -> Reply:
    return Reply(text=text, model="stub", usage={"prompt_tokens": 100,
                                                 "completion_tokens": 10},
                 latency_ms=5, tool_calls=calls)


def _call(name: str = "where", cid: str = "c1") -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


CAT = ({"name": "where", "about": "他在哪"}, {"name": "outside", "about": "天气"})


def test_不调工具就是一次调用() -> None:
    seen = []

    def chat(body, **kw):
        seen.append(kw)
        return _reply("嗯。")

    got = tools_loop.run(chat, [{"role": "user", "content": "在吗"}],
                         catalog=CAT, detail=lambda *a, **k: {}, now=T0)
    assert got.text == "嗯。"
    assert len(seen) == 1
    assert seen[0].get("tools"), "给了目录却没把工具定义发出去"


def test_调一次工具再回答() -> None:
    seen = []
    asked = []

    def chat(body, **kw):
        seen.append((len(body), bool(kw.get("tools"))))
        if len(seen) == 1:
            return _reply("", calls=[_call("where")])
        return _reply("你在公司。")

    def detail(kind, **kw):
        asked.append(kind)
        return {"loc": "公司"}

    got = tools_loop.run(chat, [{"role": "user", "content": "我在哪"}],
                         catalog=CAT, detail=detail, now=T0)
    assert got.text == "你在公司。"
    assert asked == ["where"]
    # 第二次请求里必须带着 tool 结果，而且**不再给工具**
    assert seen[1][1] is False, "第二次还在给工具，会无限循环"


def test_最多调一次() -> None:
    calls = []

    def chat(body, **kw):
        calls.append(1)
        if len(calls) == 1:
            return _reply("", calls=[_call("where", "c1"), _call("outside", "c2")])
        return _reply("好。")

    asked = []
    tools_loop.run(chat, [{"role": "user", "content": "x"}], catalog=CAT,
                   detail=lambda kind, **kw: asked.append(kind) or {}, now=T0)
    assert len(asked) == 1, "一轮里调了不止一个工具"


def test_工具炸了也要把话说完() -> None:
    def chat(body, **kw):
        if kw.get("tools"):
            return _reply("", calls=[_call("where")])
        return _reply("我不知道你在哪，但我在这儿。")

    def boom(kind, **kw):
        raise RuntimeError("数据库没了")

    got = tools_loop.run(chat, [{"role": "user", "content": "x"}], catalog=CAT,
                         detail=boom, now=T0)
    assert got.text, "工具坏了就没话说——这是最糟的失败方式"


def test_目录为空就不走这条路径() -> None:
    seen = []

    def chat(body, **kw):
        seen.append(kw)
        return _reply("嗯。")

    tools_loop.run(chat, [{"role": "user", "content": "x"}], catalog=(),
                   detail=lambda *a, **k: {}, now=T0)
    assert "tools" not in seen[0] or not seen[0]["tools"]


def test_工具定义形状固定() -> None:
    a = tools_loop.schema(CAT)
    b = tools_loop.schema(CAT)
    assert a == b
    assert a[0]["function"]["name"] == "where"
    assert a[0]["function"]["parameters"]["type"] == "object"


def test_用量要合并(db) -> None:
    def chat(body, **kw):
        if kw.get("tools"):
            return _reply("", calls=[_call("where")])
        return _reply("好。")

    got = tools_loop.run(chat, [{"role": "user", "content": "x"}], catalog=CAT,
                         detail=lambda *a, **k: {}, now=T0)
    assert got.usage["prompt_tokens"] == 200, "两次调用的用量没合并"


def test_高频对话不把工具发出去(db) -> None:
    """一句接一句的时候多等一个来回就是"卡住了"——**这条要在请求层生效**。"""
    from gits.speech import pacing

    # 平时十分钟一条，最近几条半分钟一条
    t = T0
    with Writer(db).txn() as c:
        for i, gap in enumerate([600_000] * 30 + [30_000] * 4):
            t += gap
            c.execute("INSERT INTO events(ts, recv_ts, source, kind, ext_id,"
                      " payload, lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?)",
                      (t, t, "shell", "msg_in", f"m{i}", b"hi", 0, 1, "world"))
    now = t + 5_000
    ok, why = pacing.tools_allowed(db, now=now)
    assert ok is False, why

    from gits.world import catalog
    assert catalog(db, now=now) == (), "高频时目录应该是空的"

    seen = []

    def chat(body, **kw):
        seen.append(bool(kw.get("tools")))
        return _reply("嗯。")

    tools_loop.run(chat, [{"role": "user", "content": "x"}],
                   catalog=catalog(db, now=now),
                   detail=lambda *a, **k: {}, now=now)
    assert seen == [False], "高频时不该带工具定义"
