"""上下文窗口：闲不闲得过来，值不值得换窗，整理完还在不在。

三个用法各有一组测试，因为它们的失败方式完全不同：

  idle     估错了 → 整理到一半他回来了（窗口换过、记忆断过，回不去）
  plan     判错了 → 要么白烧钱（该省不省），要么每轮全价（该换不换）
  compact  做错了 → 整理出来的东西重放不出来，或者根本不是事件
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.memory import compact as C
from gits.speech import ctxwin
from gits.core import project
from gits.core.db import DB, Writer
from gits.time.idle import MIN_GAPS, Idle, estimate, gaps_of
from gits.speech.llm import Reply

T0 = 1_800_000_000_000
MIN = 60_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _msgs(db: DB, gaps_ms: list[int], *, start: int = T0) -> None:
    t = start
    for i, g in enumerate(gaps_ms):
        Writer(db).append_event(source="shell", kind="msg_in", ext_id=f"m{i}",
                                ts=t, recv_ts=t, payload=b"hi")
        t += g


# ── idle ─────────────────────────────────────────────────────


def test_样本不够就诚实说不知道(db) -> None:
    _msgs(db, [MIN] * 5)
    got = estimate(db, T0 + 10 * MIN)
    assert got.confidence == 0.0 and got.p50_ms == 0
    assert "不够" in got.basis


def test_闲得越久_预计剩余越短(db) -> None:
    # 间隔都在 10 分钟左右
    _msgs(db, [10 * MIN] * 60)
    last = T0 + 59 * 10 * MIN
    early = estimate(db, last + 2 * MIN)
    late = estimate(db, last + 5 * MIN)
    assert late.p50_ms < early.p50_ms, "已经闲了 5 分钟，剩下的该比 2 分钟时更少"


def test_比所有历史间隔都久_只能给个很久(db) -> None:
    _msgs(db, [10 * MIN] * 60)
    got = estimate(db, T0 + 59 * 10 * MIN + 60 * MIN)
    assert got.confidence < 0.3, "历史帮不上忙了，就该承认"
    # 下界：p90 × 2 = 20 分钟，且节律因子不许把它缩小（自相矛盾）
    assert got.p50_ms >= 15 * MIN


def test_间隔分布有尾巴时_p10小于p90(db) -> None:
    _msgs(db, [5 * MIN] * 30 + [90 * MIN] * 30)
    last = T0 + 30 * 5 * MIN + 29 * 90 * MIN
    got = estimate(db, last + 3 * MIN)
    assert got.p10_ms <= got.p50_ms <= got.p90_ms
    assert got.confidence > 0.3


def test_间隔函数丢掉零和负(db) -> None:
    g = gaps_of([T0, T0, T0 + 5 * MIN, T0 + 5 * MIN])
    assert list(g) == [5 * MIN]


def test_敢不敢开始一件事用下界(db) -> None:
    i = Idle(p10_ms=10_000, p50_ms=100_000, p90_ms=200_000,
             confidence=0.8, basis="test")
    assert not i.at_least(8_000), "p10=10s，做 8s×1.5=12s 的事太冒险"
    assert i.at_least(5_000)


# ── plan ─────────────────────────────────────────────────────


def _win(db: DB, *, tokens: int = 0, turns: int = 0, zero: int = 0,
         hit: int = 0) -> int:
    wid = ctxwin.open_window(db, ts=T0)
    with Writer(db).txn() as c:
        # known_tokens 也要设：命中率的分母只算"确实知道命中数"的轮次
        c.execute("UPDATE ctx_windows SET tokens=?, turns=?, zero_streak=?,"
                  " hit_tokens=?, known_tokens=?, known_hits=? WHERE id=?",
                  (tokens, turns, zero, hit, tokens, hit, wid))
    return wid


def test_没窗口就先开一个(db) -> None:
    assert ctxwin.plan(db, now=T0).action == "open"


def test_连续两次零命中判定缓存过期(db) -> None:
    _win(db, tokens=3000, turns=6, zero=2)
    p = ctxwin.plan(db, now=T0)
    assert p.action == "new_window" and "过期" in p.reason


def test_命中率崩了立刻整理(db) -> None:
    _win(db, tokens=3000, turns=8, hit=30)
    p = ctxwin.plan(db, now=T0)
    assert p.action == "compact_force" and "命中率" in p.reason


def test_超过八成强制整理并要告诉她(db) -> None:
    _win(db, tokens=int(ctxwin.BUDGET_TOKENS * 0.85), turns=8, hit=9000)
    p = ctxwin.plan(db, now=T0)
    assert p.action == "compact_force" and p.tell_user


def test_六成但你不闲就等等(db) -> None:
    _win(db, tokens=int(ctxwin.BUDGET_TOKENS * 0.65), turns=4, hit=5000)
    busy = Idle(p10_ms=5_000, p50_ms=9_000, p90_ms=20_000, confidence=0.8, basis="t")
    assert ctxwin.plan(db, now=T0, idle=busy, compact_ms=20_000).action == "append"


def test_六成且闲得够久就整理(db) -> None:
    _win(db, tokens=int(ctxwin.BUDGET_TOKENS * 0.65), turns=4, hit=5000)
    idle = Idle(p10_ms=120_000, p50_ms=300_000, p90_ms=600_000,
                confidence=0.8, basis="t")
    p = ctxwin.plan(db, now=T0, idle=idle, compact_ms=20_000)
    assert p.action == "compact_idle"


# ── compact ──────────────────────────────────────────────────


def _chat_stub(monkeypatch, payload: str):
    calls = []

    def fake(messages, **kw):
        calls.append(kw)
        return Reply(text=payload, model="stub",
                     usage={"completion_tokens": 40, "prompt_tokens": 900},
                     latency_ms=5)

    monkeypatch.setattr(C, "chat", fake)
    return calls


GOOD = json.dumps({"summary": "他说最近开会很多，结论不清楚。",
                   "facts": ["他讨厌没有结论的会"],
                   "unfinished": [], "mood": "累"}, ensure_ascii=False)


def test_整理出来的东西本身就是事件(db, monkeypatch) -> None:
    _win(db)
    _msgs(db, [MIN] * 3)
    calls = _chat_stub(monkeypatch, GOOD)
    got = C.compact(db, now=T0 + 10 * MIN, reason="test")
    assert got is not None and got.facts == 1
    ev = db.one("SELECT kind, payload, ext_id FROM events WHERE kind='consolidate'")
    assert ev is not None and ev["ext_id"] == f"w{got.window_id - 1}"
    assert json.loads(ev["payload"])["summary"].startswith("他说最近开会很多")
    assert calls[0]["thinking"] is True, "整理是后台活，该开思考"


def test_整理完窗口就换了旧的关了(db, monkeypatch) -> None:
    old = _win(db)
    _msgs(db, [MIN] * 3)
    _chat_stub(monkeypatch, GOOD)
    got = C.compact(db, now=T0 + 10 * MIN, reason="test")
    assert db.scalar("SELECT closed_ts FROM ctx_windows WHERE id=?", (old,)) is not None
    assert ctxwin.current(db)["id"] == got.window_id != old


def test_整理能被重放出来(db, monkeypatch) -> None:
    _win(db)
    _msgs(db, [MIN] * 3)
    # 三条消息先各有抽取结果，否则重放会（正确地）报 MissingModelResult
    for r in db.q("SELECT id FROM events WHERE kind='msg_in'"):
        Writer(db).append_model_call(
            kind="extract", model="stub", event_id=r["id"],
            response=json.dumps({"summary": "一句摘要"}))
    _chat_stub(monkeypatch, GOOD)
    C.compact(db, now=T0 + 10 * MIN, reason="test")

    assert project.restore(db) == 4, "三条消息 + 一条整理"
    got = db.scalar(
        "SELECT c.content FROM node_content c JOIN nodes n ON n.id=c.node_id"
        " WHERE n.kind='self'")
    assert got == "他说最近开会很多，结论不清楚。"
    assert db.scalar("SELECT count(*) FROM nodes WHERE kind='episode'") == 3


def test_空窗口不调模型(db, monkeypatch) -> None:
    _win(db)
    calls = _chat_stub(monkeypatch, GOOD)
    assert C.compact(db, now=T0) is None
    assert calls == [], "没内容就别花钱"

