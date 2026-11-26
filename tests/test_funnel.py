"""漏斗：容量是形状，复现是动力。

用合成数据测——因为这里要验证的是**机制**，不是理论：
同一天三十条该不该算三十次？不会；隔月提起三次该不该压？应该。
这些是布尔判断，合成数据完全够用（而且不花一分钱）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.memory import funnel
from gits.core.db import DB, Writer
from gits.speech.llm import Reply
from gits.memory.recall import recall

T0 = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _episode(db: DB, nid: str, ts: int, text: str, entity: str,
             s: float = 3.0) -> None:
    from gits.core.project import entity_id

    with Writer(db).txn() as c:
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch)"
                  " VALUES(?,?,?,?,?,?,?)", (nid, "episode", ts, "world",
                                             s, 1.0, ts))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, text, ts))
        eid = entity_id(entity)
        c.execute("INSERT INTO nodes(id, kind, born_ts, scope) VALUES(?,?,?,?)"
                  " ON CONFLICT(id) DO NOTHING", (eid, "entity", ts, "world"))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (eid, entity, ts))
        c.execute("INSERT INTO edges(src, dst, w, kind, t_created, last_coact)"
                  " VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                  (nid, eid, 1.0, "mentions", ts, ts))


# ── 复现 ─────────────────────────────────────────────────────


def test_同一天三十条不算三十次(db) -> None:
    for i in range(30):
        _episode(db, f"n:{i}", T0 + i * 60_000, f"又是那个 PR 第{i}次", "PR")
    got = funnel.cluster_of(db, "PR")
    assert len(got.node_ids) == 30
    assert got.recurrence == 0, "同一个下午的三十条，是同一段叙述"
    assert not got.can_compact(), "**一次性记忆绝不能压缩**"


def test_隔一段时间又提起才算(db) -> None:
    for i in range(4):
        _episode(db, f"n:{i}", T0 + i * 20 * DAY, f"又提到那个 PR（第{i}次）", "PR")
    got = funnel.cluster_of(db, "PR")
    assert got.recurrence == 3
    assert got.weeks >= 3
    assert got.can_deepen()


def test_间隔短于稳定期就不算(db) -> None:
    # s=30 天，但每 5 天就提一次——她还没忘，这不是"重新想起来"
    for i in range(5):
        _episode(db, f"n:{i}", T0 + i * 5 * DAY, f"PR 第{i}次", "PR", 30.0)
    assert funnel.cluster_of(db, "PR").recurrence == 0


def test_验证码这种一次性内容压不出东西(db) -> None:
    for i in range(40):
        _episode(db, f"n:{i}", T0 + i * 30_000, f"验证码 {123456 + i}", "验证码")
    got = funnel.cluster_of(db, "验证码")
    assert got.recurrence == 0 and not got.can_compact()


# ── 压缩 ─────────────────────────────────────────────────────


def _stub(monkeypatch, payload: dict):
    calls = []

    def fake(messages, **kw):
        calls.append(messages)
        return Reply(text=json.dumps(payload, ensure_ascii=False), model="stub",
                     usage={"completion_tokens": 40, "prompt_tokens": 900},
                     latency_ms=3)

    monkeypatch.setattr(funnel, "chat", fake)
    return calls


def _many(db: DB, n: int = 30, *, gap_days: int = 21, s: float = 3.0) -> None:
    for i in range(n):
        _episode(db, f"n:{i}", T0 + i * gap_days * DAY,
                 f"他又因为那个 PR 跟人吵了一架（第{i}次）", "PR", s)


def test_压出来的东西带具体指称(db, monkeypatch) -> None:
    _many(db)
    _stub(monkeypatch, {"summary": "他对那个 PR 的取舍很在意，反复和人争。",
                        "why": "几十次都绕着同一个 PR"})
    got = funnel.compact(db, "PR")
    assert got is not None and got.depth == 1
    assert "PR" in got.summary
    assert got.ratio > 1.0, "压缩比应该大于 1 —— 真的压短了"


def test_被吸收的进不了检索但查得到(db, monkeypatch) -> None:
    _many(db)
    originals = set(funnel.cluster_of(db, "PR").node_ids)
    _stub(monkeypatch, {"summary": "他对那个 PR 的取舍很在意。", "why": "x"})
    got = funnel.compact(db, "PR")
    assert got is not None
    hits = {r.node_id for r in recall(db, "PR 吵架", actor="her", now_ms=T0)}
    assert got.node_id in hits, "摘要自己要能被想起来"
    assert not (originals & hits), "原件压进去之后不该再被检索到"
    # 但查得到它被谁吸收了（留作样本的那三条不算被吸收）
    absorbed = {r["node_id"] for r in db.q(
        "SELECT node_id FROM node_content WHERE absorbed_into=?", (got.node_id,))}
    assert len(absorbed) == got.children == 27
    assert absorbed < originals, "被吸收的必须是原件，不是别的东西"
    assert len(originals - absorbed) == funnel.KEEP, "留了三条样本"


def test_没有复现就不压(db, monkeypatch) -> None:
    calls = _stub(monkeypatch, {"summary": "x", "why": "y"})
    for i in range(40):
        _episode(db, f"n:{i}", T0 + i * 60_000, f"汉堡店第{i}次被提起", "汉堡店")
    assert funnel.compact(db, "汉堡店") is None
    assert calls == [], "复现次数不够，连模型都不该调"


def test_压出来的东西能重放(db, monkeypatch) -> None:
    from gits.core import project

    _many(db)
    _stub(monkeypatch, {"summary": "他对那个 PR 的取舍很在意。", "why": "x"})
    got = funnel.compact(db, "PR")
    assert got is not None
    n = project.restore(db)
    assert n >= 1
    row = db.one("SELECT content FROM node_content WHERE node_id=?", (got.node_id,))
    assert row["content"] == "他对那个 PR 的取舍很在意。"


# ── 调度 ─────────────────────────────────────────────────────


def test_调度只看容量和遗忘_不看日历(db) -> None:
    _many(db, n=30)
    jobs = funnel.plan(db, now=T0 + 29 * 21 * DAY + DAY)
    assert [j[0] for j in jobs] == ["PR"]
    assert "活着" in jobs[0][1]


def test_正在被遗忘的簇不压(db) -> None:
    # 30 条都老得差不多想不起来了：压它只是把模糊的话留住
    for i in range(30):
        _episode(db, f"n:{i}", T0 + i * 21 * DAY, f"PR 第{i}次", "PR", 1.0)
    long_after = T0 + 30 * 21 * DAY + 400 * DAY
    assert funnel.plan(db, now=long_after) == []


def test_没有复现的簇排不上(db) -> None:
    for i in range(40):
        _episode(db, f"n:{i}", T0 + i * 60_000, f"汉堡店第{i}次", "汉堡店")
    assert funnel.plan(db, now=T0 + 60_000) == []


def test_深度直方图能看出来是不是漏斗(db, monkeypatch) -> None:
    _many(db)
    _stub(monkeypatch, {"summary": "他对那个 PR 的取舍很在意。", "why": "x"})
    funnel.compact(db, "PR")
    hist = funnel.depth_histogram(db)
    assert hist.get(0, 0) > hist.get(1, 0), "原件比摘要多——这才叫漏斗"
