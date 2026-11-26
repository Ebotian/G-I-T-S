"""社区经验里最值钱的两条，这里各测一组。

  出口清洗   一条微信停在逗号上比不发更糟；交差腔和裸链接在微信里很难看
  反刍闸     没答案的问题会自我喂养——**我们这里真的有一条这样的回路**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.core.project import entity_id
from gits.memory.graph import add_edge
from gits.speech import unresolved
from gits.speech.clean import clean, full_stop, strip_links, strip_meta

T0 = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


# ── 出口清洗 ────────────────────────────────────────────────


def test_半截话退到整句() -> None:
    """max_tokens 顶头会拦腰掐断——停在逗号上比不发更糟。"""
    got = full_stop("至于信息素从哪儿来，它们大概靠腹部腺体分泌，还有")
    assert got.endswith("分泌。") or got.endswith("来，") is False
    assert not got.endswith(("，", "、"))


def test_已经完整的不动() -> None:
    assert full_stop("好。") == "好。"
    assert full_stop("真的？") == "真的？"


def test_超帽先裁再收笔() -> None:
    got = full_stop("这是一句话。" * 100, cap=40)
    assert len(got) <= 40 and got.endswith("。")


def test_链接只留字() -> None:
    assert strip_links("[世界统计](https://x.com/a) 上是这样") == "世界统计 上是这样"
    assert "http" not in strip_links("看这个 https://x.com/a 很有趣")


def test_去掉交差腔() -> None:
    """「我查了一下」是模型在汇报自己在干活，不是在说话。"""
    assert strip_meta("我查了一下，蚂蚁靠触角认路。") == "蚂蚁靠触角认路。"
    assert strip_meta("翻到了：猫舌头是空心的。") == "猫舌头是空心的。"


def test_不误伤正常开头() -> None:
    assert strip_meta("嗯。") == "嗯。"
    assert strip_meta("我不知道，但我猜是触角。") == "我不知道，但我猜是触角。"


def test_三道闸的顺序() -> None:
    got = clean("我查了一下，[这个](https://x.com) 是这样，还有")
    assert "查了一下" not in got and "http" not in got
    assert not got.endswith("，")


# ── 反刍闸 ──────────────────────────────────────────────────


def _dangle(db: DB, name: str, times: int = 3) -> str:
    eid = entity_id(name)
    for i in range(times):
        ts = T0 + i * DAY
        nid = f"n:{name}-{i}"
        with Writer(db).txn() as c:
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope, s, r,"
                      " last_touch) VALUES(?,?,?,?,?,?,?)",
                      (nid, "episode", ts, "world", 1.0, 1.0, ts))
            c.execute("INSERT INTO nodes(id, kind, born_ts, scope)"
                      " VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, "entity", ts, "world"))
            c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                      " VALUES(?,?,?)", (eid, name, ts))
            add_edge(c, src=nid, dst=eid, kind="mentions", ts=ts, w=1.0)
    return eid


def test_问不出东西的事会被翻篇(db) -> None:
    """这条回路我们真的有：没有 claims 的实体 unexcluded 一直是 1.0。"""
    eid = _dangle(db, "看病")
    assert [c.entity for c in unresolved.scan(db, now=T0 + 2 * DAY)] == [eid]

    for i in range(unresolved.STRIKES_TO_BURN):
        unresolved.record_use(db, eid, known=0, now=T0 + 2 * DAY + i)
    assert unresolved.burned(db, eid, now=T0 + 2 * DAY + HOUR)
    assert unresolved.scan(db, now=T0 + 2 * DAY + HOUR) == [], "翻篇了还排前面"


def test_问出新东西就不算翻篇(db) -> None:
    eid = _dangle(db, "看病")
    for i in range(unresolved.STRIKES_TO_BURN):
        unresolved.record_use(db, eid, known=0, now=T0 + 2 * DAY + i)
    # 这回真问出来了（known 从 0 变成 1）
    unresolved.record_use(db, eid, known=1, now=T0 + 2 * DAY + HOUR)
    assert not unresolved.burned(db, eid, now=T0 + 2 * DAY + HOUR)


def test_冷却之后旧账不算(db) -> None:
    eid = _dangle(db, "看病")
    for i in range(unresolved.STRIKES_TO_BURN):
        unresolved.record_use(db, eid, known=0, now=T0 + 2 * DAY + i)
    much_later = T0 + 2 * DAY + unresolved.BURN_HOURS * HOUR * 4
    assert not unresolved.burned(db, eid, now=much_later), "翻篇不该是永久的"
