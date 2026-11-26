"""前缀的稳定性：**测出来的，不是设计出来的。**

A/B 段"稳定"是意图；缓存只认字节。这个文件让"前缀稳不稳"变成可查的数字。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.speech import shape

HOUR = 3_600_000
T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_只记指纹_不记正文(db) -> None:
    """正文是内容，不该进日志。"""
    shape.record(db, now=T0, a="我是苏清语。", b="我们在", c="他说在吗")
    raw = db.scalar("SELECT payload FROM events WHERE kind='ctx_shape'")
    assert "苏清语" not in raw.decode()
    assert len(raw.decode()) < 120


def test_同一个窗口里_B_变了几次(db) -> None:
    for i, b in enumerate(("B1", "B1", "B2")):
        shape.record(db, now=T0 + i * 60_000, a="A", b=b, c=f"C{i}", window_id=7)
    s = shape.stats(db, now=T0 + 5 * 60_000)
    assert s["b_per_window"] == 2
    assert s["a_versions"] == 1
    assert s["c_versions"] == 3


def test_C_每轮都变是正常的(db) -> None:
    for i in range(4):
        shape.record(db, now=T0 + i * 60_000, a="A", b="B", c=f"C{i}", window_id=7)
    assert shape.stats(db, now=T0 + HOUR)["b_per_window"] == 1
    assert shape.diagnose(db, now=T0 + HOUR) == [], "C 段每轮变不该报警"


def test_窗口之内_B_变了要报(db) -> None:
    """**结构性问题**：不用等命中率低，它本身就说明前缀每轮断。"""
    for i, b in enumerate(("B1", "B2")):
        shape.record(db, now=T0 + i * 60_000, a="A", b=b, c=f"C{i}", window_id=7)
    for i in range(4):
        shape.record(db, now=T0 + (i + 2) * 60_000, a="A", b="B2", c=f"D{i}",
                     window_id=7)
    out = shape.diagnose(db, now=T0 + HOUR)
    assert any("B 段在同一个窗口里变了" in line for line in out), out


def test_轮数太少不下结论(db) -> None:
    """冷启动的低命中不是问题——三轮回合说明不了什么。"""
    for i in range(2):
        shape.record(db, now=T0 + i * 60_000, a="A", b=f"B{i}", c="C",
                     window_id=1)
    assert shape.diagnose(db, now=T0 + HOUR) == []


def test_没有数据不崩(db) -> None:
    assert shape.stats(db, now=T0)["turns"] == 0
    assert shape.diagnose(db, now=T0) == []
    assert "命中=?" in shape.line(db, now=T0)


# ── B 段必须逐字不变 ────────────────────────────────────────


def _seed_graph(db: DB, *, touch: int, n: int = 5) -> None:
    from gits.core.project import entity_id

    with Writer(db).txn() as c:
        for i in range(n):
            name = f"事{i}"
            nid = entity_id(name)
            c.execute("INSERT OR IGNORE INTO nodes(id, kind, born_ts, scope, s,"
                      " r, last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "entity", T0 - 86_400_000, "world", 1.0, 0.5, touch))
            c.execute("INSERT OR IGNORE INTO node_content(node_id, content,"
                      " created_ts) VALUES(?,?,?)", (nid, name, touch))


def test_B_段同一小时逐字不变(db) -> None:
    """它声称"一小时内逐字不变"。以前 hubs 按 last_touch 排序、情绪拿 now 进去，
    **每轮都可能变**——system 前缀每轮断，而没有任何东西报错。"""
    from gits.speech.respond import relation

    _seed_graph(db, touch=T0)
    early = relation(db, now=T0 + 60_000)
    late = relation(db, now=T0 + 59 * 60_000)
    assert early == late, "同一小时内 B 段变了"


def test_这一小时里又碰了一个实体_B_段仍然不变(db) -> None:
    """**这是真实场景**：他每说一句，就会有实体被 touch。"""
    from gits.speech.respond import relation

    _seed_graph(db, touch=T0)
    before = relation(db, now=T0 + 60_000)
    with Writer(db).txn() as c:
        c.execute("UPDATE nodes SET last_touch=? WHERE id="
                  "(SELECT id FROM nodes WHERE kind='entity' LIMIT 1)",
                  (T0 + 30 * 60_000,))
    after = relation(db, now=T0 + 31 * 60_000)
    assert before.hubs == after.hubs, f"碰了一下实体，B 段就变了：{before.hubs} → {after.hubs}"


def test_下一小时可以变(db) -> None:
    """折到整点不是"永远不变"——整点换了，允许换。"""
    from gits.speech.respond import relation

    _seed_graph(db, touch=T0)
    a = relation(db, now=T0 + 59 * 60_000)
    b = relation(db, now=T0 + 61 * 60_000)
    assert a.bucket != b.bucket


def test_情绪的措辞一小时内也不能翻(db) -> None:
    """实测：同一条情绪数据，T+0 说"最近偏高兴"，T+55 就说"最近有点高兴，但很杂"。
    两句都是真话，但**前缀不能翻**——B 段必须折到整点。
    （这条是用真实数据找到的：折整点之前它确实会翻。）"""
    from gits.social import affect
    from gits.speech.respond import relation

    for i in range(8):
        affect.observe(db, "her", angle="joy", intensity=2,
                       ts=T0 - (8 - i) * 60_000)
    # 先确认这份数据确实在两句话之间跨过分界
    assert (affect.line(db, "her", now=T0)
            != affect.line(db, "her", now=T0 + 55 * 60_000)), "样本没跨过分界，测不出东西"
    early = relation(db, now=T0)
    late = relation(db, now=T0 + 55 * 60_000)
    assert early.affect == late.affect, f"同一小时里情绪措辞翻了：{early.affect} → {late.affect}"
