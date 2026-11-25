"""实例之间：上限、回合、投递、观测。

每个测试对应一条已经定下的规则，失败就是那条规则没落地：

  上限    主动有上限，**回应没有**——否则被封顶的人只能沉默
  回合    沉默也是回应；它不违约，但要能被数出来
  投递    消息进双方各自的私有域，**第三方什么都看不到**
  观测    没发生要有记录，选项没露出和选择不开口必须能分开
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import actors, attention, peer
from gits.db import DB, Writer
from gits.respond import recent
from gits.scope import self_of

T0 = 1_800_000_000_000
HOUR = 3_600_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    actors.ensure(d, "her", is_main=True)
    actors.ensure(d, "peer1")
    actors.ensure(d, "peer2")
    yield d
    d.close()


# ── 上限 ─────────────────────────────────────────────────────


def test_主动有上限_回应没有(db) -> None:
    for i in range(peer.DAILY_COUNT):
        s = peer.say(db, "peer1", "peer2", f"第{i}句", now=T0)
        assert s.outbox_id > 0 and not s.drafted
    ok, why = peer.can_initiate(db, "peer1", "peer2", now=T0)
    assert not ok and "上限" in why
    # 封顶了，但**回应**照样能发
    s = peer.say(db, "peer2", "peer1", "我回你", now=T0, free=True)
    assert s.outbox_id > 0, "回应不占额度，双向不能断"


def test_封顶之后的话进草稿(db) -> None:
    for i in range(peer.DAILY_COUNT):
        peer.say(db, "peer1", "peer2", f"第{i}句", now=T0)
    s = peer.say(db, "peer1", "peer2", "这句说不出口", now=T0)
    assert s.drafted and len(peer.drafts(db, "peer1", now=T0)) == 1


def test_草稿队列有上限_溢出转成没说出口的记忆(db) -> None:
    for i in range(peer.DAILY_COUNT):
        peer.say(db, "peer1", "peer2", f"第{i}句", now=T0)
    for i in range(3):
        peer.say(db, "peer1", "peer2", f"草稿{i}", now=T0 + i)
    assert len(peer.drafts(db, "peer1", now=T0 + 10)) == peer.MAX_DRAFTS
    unsaid = [r["payload"].decode() for r in db.q(
        "SELECT payload FROM events WHERE kind='unsaid'")]
    assert unsaid == ["草稿0"], "溢出的最老那条转成记忆，不是消失"


def test_滚动二十四小时不是自然日(db) -> None:
    for i in range(peer.DAILY_COUNT):
        peer.say(db, "peer1", "peer2", f"第{i}句", now=T0)
    assert not peer.can_initiate(db, "peer1", "peer2", now=T0 + HOUR)[0]
    assert peer.can_initiate(db, "peer1", "peer2", now=T0 + 25 * HOUR)[0], \
        "过了窗口就该能说——不需要等到某个整点"


def test_钱的上限独立于条数(db) -> None:
    peer.say(db, "peer1", "peer2", "很贵的一句", now=T0, cost=0.5)
    ok, why = peer.can_initiate(db, "peer1", "peer2", now=T0)
    assert not ok and "元" in why


# ── 投递 ─────────────────────────────────────────────────────


def test_消息进双方记忆_第三方不知道(db) -> None:
    peer.say(db, "peer1", "peer2", "我最近睡不好", now=T0)
    assert peer.deliver(db, now=T0) == 1

    assert [t for _w, t in recent(db, actor="peer1")] == ["我最近睡不好"]
    assert [t for _w, t in recent(db, actor="peer2")] == ["我最近睡不好"]
    assert recent(db, actor="her") == (), "她不该知道这两个人说了什么"


def test_重复投递不会重复进记忆(db) -> None:
    peer.say(db, "peer1", "peer2", "一句话", now=T0)
    peer.deliver(db, now=T0)
    assert peer.deliver(db, now=T0) == 0
    assert len(recent(db, actor="peer2")) == 1


# ── 回合 ─────────────────────────────────────────────────────


def test_收到就有回合(db) -> None:
    peer.say(db, "peer1", "peer2", "在吗", now=T0)
    peer.deliver(db, now=T0)
    got = peer.received(db, "peer2")
    assert len(got) == 1 and got[0]["payload"].decode() == "在吗"
    assert peer.received(db, "peer1") == []


def test_沉默也是回应_回合就不再来(db) -> None:
    peer.say(db, "peer1", "peer2", "在吗", now=T0)
    peer.deliver(db, now=T0)
    ev = peer.received(db, "peer2")[0]
    peer.silent(db, "peer2", ev, now=T0 + HOUR)
    assert peer.received(db, "peer2") == []
    assert peer.observe(db)["silent"] == 1


def test_回应之后回合也不再来(db) -> None:
    peer.say(db, "peer1", "peer2", "在吗", now=T0)
    peer.deliver(db, now=T0)
    turn = peer.received(db, "peer2")[0]
    peer.say(db, "peer2", "peer1", "在", now=T0, free=True, reply_to=turn)
    assert peer.received(db, "peer2") == [], "回应之后回合就用掉了，不该回两遍"


# ── 观测 ─────────────────────────────────────────────────────


def test_选项没露出和选择不开口必须能分开(db) -> None:
    obs = peer.observe(db)
    assert obs["affordances"] == 0 and obs["first_contact_ts"] is None
    peer.affordance(db, "peer1", now=T0)
    peer.affordance(db, "peer1", now=T0 + HOUR)
    obs = peer.observe(db)
    assert obs["affordances"] == 2, "露过两次"
    assert obs["first_contact_ts"] is None, "露过但没开口——这才是数据"


def test_第一次开口能被查出来(db) -> None:
    peer.say(db, "peer1", "peer2", "第一次说话", now=T0)
    obs = peer.observe(db)
    assert obs["first_contact_ts"] == T0
    assert obs["by_pair"] == {"peer1->peer2": 1}


# ── 漂移 ─────────────────────────────────────────────────────


def test_漂移反相且限速(db) -> None:
    attention.seed(db, "peer1", {"开会": 1.0, "咖啡": 1.0})
    attention.seed(db, "peer2", {"开会": 1.0, "咖啡": 1.0})
    d = attention.step(db, pair=("peer1", "peer2"), pressure=1.0, now=T0)
    assert d["peer1"]["开会"] > 0 and d["peer2"]["开会"] < 0, "反相"
    assert abs(d["peer1"]["开会"]) <= attention.MAX_DAILY + 1e-9, "限速"


def test_漂移有界(db) -> None:
    attention.seed(db, "peer1", {"开会": 1.0})
    attention.seed(db, "peer2", {"开会": 1.0})
    for i in range(200):
        attention.step(db, pair=("peer1", "peer2"), pressure=1.0, now=T0 + i)
    w = attention.weights(db, "peer1")["开会"]
    assert w <= 2.0 + 1e-9, "上界 %s" % w


def test_没有推动就慢慢回到初始值(db) -> None:
    attention.seed(db, "peer1", {"开会": 1.0})
    with Writer(db).txn() as c:
        c.execute("UPDATE attention SET weight=1.8 WHERE actor_id='peer1'")
    for i in range(100):
        attention.step(db, pair=("peer1", "peer1"), now=T0 + i)
    assert attention.weights(db, "peer1")["开会"] < 1.8


def test_漂移本身是事件且只在私有域(db) -> None:
    attention.seed(db, "peer1", {"开会": 1.0})
    attention.seed(db, "peer2", {"开会": 1.0})
    attention.step(db, pair=("peer1", "peer2"), pressure=0.5, now=T0)
    ev = db.one("SELECT scope, payload FROM events WHERE kind='drift'")
    assert ev is not None and ev["scope"] == self_of("peer1")
    assert json.loads(ev["payload"])["pair"] == ["peer1", "peer2"]
