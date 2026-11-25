"""分层同步：不同东西不同频率，因为消费方不同。

以及一个容易被忽略的后果：**计算依赖各端不同，产物就不同**。
所以产物必须带 producer，否则两端的东西会被静默混用。
"""

from __future__ import annotations

import pytest

from gits.sync import (
    BY_NAME, STREAMS, TIERS, compatible, echo_merge, plan, producer_of,
)

MIN = 60_000
HOUR = 3_600_000


def test_分层覆盖了该传和不该传的() -> None:
    syncable = {s.name for s in STREAMS if s.syncable}
    assert "events" in syncable
    assert "self_narrative" in syncable
    # 投影和不可移植的产物不传
    for name in ("nodes", "edges", "embedding", "acoustic"):
        assert name not in syncable, name


def test_每一层都有说明() -> None:
    for s in STREAMS:
        assert s.tier in TIERS
        assert s.why, f"{s.name} 没写为什么"


def test_人格比投影优先() -> None:
    order = [s.name for s in sorted(STREAMS, key=lambda x: (x.tier, x.staleness_max_ms))]
    assert order.index("self_narrative") < order.index("events")


def test_容忍度高于节点能力时必须明说做不到() -> None:
    """工作机 30 分钟才同步一次，而人格文件要 5 分钟内新鲜——做不到就得说。"""
    plans = {p.stream: p for p in plan("office", sync_period_ms=30 * MIN,
                                       freshness_ms=HOUR, pending_up={})}
    p = plans["self_narrative"]
    assert p.action == "skip"
    assert "做不到" in p.reason and "本地生产" in p.reason


def test_云上能同步就同步() -> None:
    plans = {p.stream: p for p in plan("cloud", sync_period_ms=MIN,
                                       freshness_ms=2 * HOUR,
                                       pending_up={"events": 12})}
    assert plans["events"].action == "push"
    assert plans["self_narrative"].action == "pull"
    assert plans["nodes"].action == "skip"


def test_没有待传就不推() -> None:
    plans = {p.stream: p for p in plan("cloud", sync_period_ms=MIN,
                                       freshness_ms=0, pending_up={})}
    assert plans["events"].action != "push"
    assert plans["events"].action == "skip", plans["events"].reason


def test_删除比新增更急() -> None:
    """隐私动作的容忍度必须小于普通事件——删了就该尽快到处都删掉。"""
    assert BY_NAME["redactions"].staleness_max_ms < BY_NAME["events"].staleness_max_ms


# ── 产物身份 ────────────────────────────────────────────────
def test_换模型就是换身份() -> None:
    a = producer_of("office", "bge-m3", "v1")
    b = producer_of("office", "bge-m3", "v2")
    assert a != b
    assert not compatible(a, b), "不能把 v1 的向量和 v2 的混在一个索引里"


def test_同节点同版本才算兼容() -> None:
    a = producer_of("office", "bge-m3", "v1")
    assert compatible(a, producer_of("office", "bge-m3", "v1"))
    assert not compatible(a, producer_of("cloud", "bge-m3", "v1"))


# ── 回环合并 ────────────────────────────────────────────────
def test_自己推上去的事件拉回来不能算两遍() -> None:
    pending = [{"source": "mic", "ext_id": "a"}, {"source": "mic", "ext_id": "b"}]
    pulled = [{"source": "mic", "ext_id": "a", "id": 101},
              {"source": "wechat", "ext_id": "z", "id": 102}]
    left = echo_merge(pending, pulled)
    assert [e["ext_id"] for e in left] == ["b"]


def test_回环合并不是删除() -> None:
    """被合并掉的是"我在本地先记下的那条"，云端那份才是 canonical。"""
    pending = [{"source": "mic", "ext_id": "a"}]
    assert echo_merge(pending, [{"source": "mic", "ext_id": "a"}]) == []
