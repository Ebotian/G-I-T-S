"""情绪接进记忆的四个乘子。

顺序就是这四个，每一层都能**单独关掉**（gain=0 时精确退化成纯 FSRS）：

  1. 节点带自己的情感标签（编码时写进去）
  2. 同日重复 → w17/w18 的短期巩固（**和情绪无关**，补的是那个洞）
  3. 事件情感强度 → 编码增益（arousal → consolidation）
  4. 她此刻的心情 → 检索排序（mood-congruent recall）

重点盯的两种错法：

  把 3 和 4 混成一个     → 难过 → 记得更牢 → 想起更多 → 更难过（回路）
  同日重复也当成独立重述 → 漏斗的复现数被一个下午的三十条刷满
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.core.project import apply_payload
from gits.memory import fsrs
from gits.memory.recall import recall, reindex
from gits.social import affect

T0 = 1_800_000_000_000
HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _apply(db: DB, ext: str, ts: int, *, entities: list[str],
           affect_pair: tuple[str, int] | None = None,
           summary: str = "一件事") -> str:
    payload = {"summary": summary,
               "entities": [{"name": n, "kind": "topic"} for n in entities],
               "claims": []}
    if affect_pair:
        payload["affect"] = {"angle": affect_pair[0], "intensity": affect_pair[1]}
    row = {"kind": "msg_in", "source": "shell", "ext_id": ext, "ts": ts,
           "scope": "world"}
    with Writer(db).txn() as c:
        return apply_payload(c, row, payload)


# ── 1. 节点带情感标签 ────────────────────────────────────────


def test_节点记住自己的情感(db) -> None:
    nid = _apply(db, "m1", T0, entities=["开会"], affect_pair=("anger", 3))
    row = db.one("SELECT affect_angle, affect_intensity FROM nodes WHERE id=?",
                 (nid,))
    assert row["affect_angle"] == "anger" and row["affect_intensity"] == 3


def test_没有情绪的节点是空的(db) -> None:
    nid = _apply(db, "m1", T0, entities=["开会"])
    row = db.one("SELECT affect_angle FROM nodes WHERE id=?", (nid,))
    assert row["affect_angle"] is None


# ── 2. 同日重复 ─────────────────────────────────────────────


def test_短期因子取自_w17_w18(db) -> None:
    expect = __import__("math").exp(fsrs.W[17] * fsrs.W[18])
    assert fsrs.short_term_factor() == pytest.approx(expect)
    assert 1.3 < fsrs.short_term_factor() < 1.5, "社区拟合值是 1.39 左右"


def test_同日重复会巩固但递减(db) -> None:
    assert fsrs.same_day_boost(1) > fsrs.same_day_boost(2) > fsrs.same_day_boost(5)
    assert fsrs.same_day_boost(5) > 1.0, "但它不该归零——重复确实巩固了一次"


def test_一个下午三十条不会把稳定性顶到三十倍(db) -> None:
    for i in range(30):
        _apply(db, f"m{i}", T0 + i * 60_000, entities=["那个 PR"])
    s = db.scalar("SELECT s FROM nodes WHERE kind='entity'")
    # 同一天只巩固前 SHORT_TERM_STEPS 次，之后不再加深
    assert s < 3.0, f"三十条把稳定性顶到 {s} 了"
    assert s > 1.0, "但也不该毫无贡献"


def test_隔天就重新计数(db) -> None:
    _apply(db, "m1", T0, entities=["那个 PR"])
    _apply(db, "m2", T0 + DAY + HOUR, entities=["那个 PR"])
    row = db.one("SELECT touch_n FROM nodes WHERE kind='entity'")
    assert row["touch_n"] == 1, "新的一天从头数"


# ── 3. 情感强度 → 编码 ──────────────────────────────────────


def test_强烈的记忆编码得更牢(db) -> None:
    weak = _apply(db, "m1", T0, entities=[], affect_pair=("anger", 1))
    strong = _apply(db, "m2", T0, entities=[], affect_pair=("anger", 3))
    s1 = db.scalar("SELECT s FROM nodes WHERE id=?", (weak,))
    s2 = db.scalar("SELECT s FROM nodes WHERE id=?", (strong,))
    # 强度 1 映射到 0 增益（轻微情绪不算唤醒），所以它精确等于基线
    assert s1 == pytest.approx(1.0)
    assert s2 > s1


def test_gain_为零时精确退化成纯_FSRS(db) -> None:
    assert fsrs.arousal_boost(3, gain=0.0) == 1.0
    assert fsrs.arousal_boost(None) == 1.0
    assert fsrs.arousal_boost(1) == 1.0, "强度 1 = 不激动 = 不增益"


def test_增益有界(db) -> None:
    assert fsrs.arousal_boost(3) <= 1.0 + fsrs.AROUSAL_GAIN


# ── 4. 心情 → 检索 ─────────────────────────────────────────


def test_一致性按圆盘几何算(db) -> None:
    cells = {("anger", 2): 0.9}
    assert fsrs.mood_congruence("anger", 2, cells) > 0.8
    assert fsrs.mood_congruence("joy", 1, cells) < 0.6, "对面不该算一致"


def test_心情一致的内容排在前面(db) -> None:
    a = _apply(db, "m1", T0, entities=[], affect_pair=("anger", 2),
               summary="吵架那件事")
    b = _apply(db, "m2", T0, entities=[], affect_pair=("joy", 2),
               summary="吵架那天其实挺好")
    reindex(db)
    affect.observe(db, "her", angle="anger", intensity=2, ts=T0)

    got = [r.node_id for r in recall(db, "吵架", actor="her", now_ms=T0)]
    assert got[0] == a, "她现在在气头上，先想起的是气的那一条"
    assert set(got) == {a, b}, "只调排序，不删候选"


def test_心情不影响编码(db) -> None:
    """她此刻的心情**不该**改变「这条记忆存得多牢」。

    混在一起会形成回路：难过 → 记得更牢 → 想起更多 → 更难过。
    """
    _apply(db, "m1", T0, entities=[], summary="中性的一件事")
    before = db.scalar("SELECT s FROM nodes WHERE kind='episode'")
    affect.observe(db, "her", angle="sadness", intensity=3, ts=T0)
    after = db.scalar("SELECT s FROM nodes WHERE kind='episode'")
    assert before == after


def test_没有情绪记忆时不影响排序(db) -> None:
    a = _apply(db, "m1", T0, entities=[], summary="开会开到吐")
    b = _apply(db, "m2", T0, entities=[], summary="开会又一天")
    reindex(db)
    affect.observe(db, "her", angle="anger", intensity=3, ts=T0)
    got = {r.node_id for r in recall(db, "开会", actor="her", now_ms=T0)}
    assert got == {a, b}


# ── 检索即强化（spacing effect）──────────────────────────────


def test_强化让稳定性涨(db) -> None:
    s, d = fsrs.reinforce(3.0, 5.0, 0.5)
    assert s > 3.0 and 1.0 <= d <= 10.0


def test_越难想起来涨得越多(db) -> None:
    """间隔效应的核心：r 越低还想起得来，巩固越强。"""
    low = fsrs.reinforce(3.0, 5.0, 0.2)[0]
    high = fsrs.reinforce(3.0, 5.0, 0.95)[0]
    assert low > high > 3.0


def test_单次强化有上限(db) -> None:
    """FSRS 是为**主动复习**设计的：r=0.2 时它给出 36 倍。

    被动想起不该等价于一次刻意的复习。
    """
    s, _d = fsrs.reinforce(3.0, 5.0, 0.0)
    assert s == pytest.approx(3.0 * fsrs.REINFORCE_MAX_GAIN)


def test_没有难度的节点用中值(db) -> None:
    a, d = fsrs.reinforce(3.0, None, 0.5)
    assert d == pytest.approx(fsrs.next_difficulty(fsrs.DEFAULT_D, 3), abs=1e-9)


def test_真的用上才强化(db) -> None:
    a = _apply(db, "m1", T0, entities=[], summary="开会开到吐")
    reindex(db)
    later = T0 + 5 * DAY
    hits = recall(db, "开会", actor="her", now_ms=later)
    before = db.scalar("SELECT s FROM nodes WHERE id=?", (a,))
    assert db.scalar("SELECT s FROM nodes WHERE id=?", (a,)) == before, \
        "recall 是读路径，不该改状态"

    from gits.memory.recall import reinforce_hits

    assert reinforce_hits(db, hits, now_ms=later) == 1
    assert db.scalar("SELECT s FROM nodes WHERE id=?", (a,)) > before


def test_r_接近一时强化无效(db) -> None:
    """刚创建就想起，等于没有练习——FSRS 的本意，不是 bug。"""
    s, _d = fsrs.reinforce(3.0, 5.0, 1.0)
    assert s == pytest.approx(3.0)


def test_只强化前几条(db) -> None:
    ids = [_apply(db, f"m{i}", T0, entities=[], summary=f"开会第{i}次")
           for i in range(8)]
    reindex(db)
    hits = recall(db, "开会", actor="her", now_ms=T0, k=8)
    from gits.memory.recall import reinforce_hits

    assert reinforce_hits(db, hits, now_ms=T0 + DAY, limit=3) == 3


# ── 端到端：情绪强的记忆确实留存更久 ─────────────────────────


def test_情绪强的记忆两周后更容易被想起(db) -> None:
    low = _apply(db, "m1", T0, entities=[], summary="开会这件事一般般",
                 affect_pair=("joy", 1))
    high = _apply(db, "m2", T0, entities=[], summary="开会这件事吵起来了",
                  affect_pair=("anger", 3))
    reindex(db)

    s_low = db.scalar("SELECT s FROM nodes WHERE id=?", (low,))
    s_high = db.scalar("SELECT s FROM nodes WHERE id=?", (high,))
    assert s_high > s_low

    later = recall(db, "开会", actor="her", now_ms=T0 + 14 * DAY, k=10)
    scores = {r.node_id: r.score for r in later}
    assert scores.get(high, 0) > scores.get(low, 0), f"两周后情绪强的那条该排前面：{[(r.node_id, round(r.score, 3)) for r in later]}"


def test_强化之后更耐忘(db) -> None:
    a = _apply(db, "m1", T0, entities=[], summary="开会开到吐")
    reindex(db)
    from gits.memory.recall import reinforce_hits

    hits = recall(db, "开会", actor="her", now_ms=T0 + 5 * DAY)
    reinforce_hits(db, hits, now_ms=T0 + 5 * DAY)
    s_after = db.scalar("SELECT s FROM nodes WHERE id=?", (a,))

    # 同样的时间之后，强化过的 r 更高
    from gits.memory.fsrs import retrievability

    assert retrievability(30, s_after) > retrievability(30, 1.0)
