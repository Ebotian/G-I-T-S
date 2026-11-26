
"""三处线上真出过的问题：B 段每轮变、工具回音缺失、affect 多一列。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech.llm import Reply

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_B_段不随记忆条数变化(db) -> None:
    """**"记下 N 件事"每抽取一条就 +1，于是 B 段每轮必变、前缀每轮断。**
    实测 13 轮里 B 变了 8 次，命中率掉到 27%。"""
    from gits.speech.respond import relation

    # 先有一条，让措辞已经跨过"还没记下什么"那一次（那次只该发生一次）
    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r)"
                  " VALUES('n:seed', 'episode', ?, 'world', 1, 1)", (T0,))
    before = relation(db, now=T0)
    with Writer(db).txn() as c:
        for i in range(5):
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r)"
                      " VALUES(?, 'episode', ?, 'world', 1, 1)",
                      (f"n:{i}", T0))
    after = relation(db, now=T0)
    assert before.summary == after.summary, f"多记了五件事，B 段就变了：{before.summary} → {after.summary}"


def test_工具声明几个就回几个(db) -> None:
    """模型一口气要查三样，我们只执行一个——但**每条 tool_call_id 都要有回音**，
    否则第二次请求 400，那一轮整条回复就丢了（线上真发生过）。"""
    from gits.speech import tools_loop

    bodies = []
    calls = [{"id": f"c{i}", "function": {"name": "where", "arguments": "{}"}}
             for i in range(3)]

    def chat(messages, **kw):
        bodies.append(messages)
        if len(bodies) == 1:
            return Reply(text="", model="stub", usage={}, latency_ms=1,
                         tool_calls=calls)
        return Reply(text="在外面。", model="stub", usage={}, latency_ms=1)

    tools_loop.run(chat, [{"role": "user", "content": "在吗"}],
                   catalog=({"name": "where", "about": "在哪"},),
                   detail=lambda name, **kw: {"ok": True}, now=T0)
    second = bodies[1]
    declared = [c["id"] for m in second if m.get("tool_calls")
                for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in second if m.get("role") == "tool"]
    assert declared == answered, f"声明 {declared}，只回了 {answered}"


def test_schema_check_能看见多出来的列(tmp_path: Path, capsys) -> None:
    """生产库 affect 多了一列 last_ts，而代码只插四列——
    每一条带情绪的抽取都在这里失败。这个检查就是为这类事写的。"""
    import argparse
    import sqlite3

    from gits.cli import cmd_schema_check

    path = tmp_path / "live.db"
    fresh = DB(tmp_path / "fresh.db")
    fresh.close()
    src = sqlite3.connect(tmp_path / "fresh.db")
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.execute("CREATE TABLE legacy_only(x INTEGER)")
    dst.commit()
    dst.close()

    # 干净库：一致
    assert cmd_schema_check(argparse.Namespace(db=str(path))) == 0
    # 制造漂移：删一列做不到，就加一张表 + 改一列类型
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE character ADD COLUMN surprise TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()
    assert cmd_schema_check(argparse.Namespace(db=str(path))) == 1
    out = capsys.readouterr().out
    assert "character.surprise" in out
