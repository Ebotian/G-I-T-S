"""多端调度：三条硬约束 + 状态机。

所有数字都来自实测画像（docs/research/multi-node.md §1）：
    ECS  2C / 1.6 GB / 无 GPU / 40 GB 盘
    PC   12C / 30 GB / RTX 3050 6 GB / 638 GB 盘
"""

from __future__ import annotations

import pytest

from gits.sched import (
    Decision, Health, Node, Phase, Task, eligible, phase_of, place, should_preempt,
)

HOUR = 3_600_000
MIN = 60_000

ECS = Node(id="cloud", cpu=2, ram_mb=1600, ram_budget_mb=500, resident_mb=250,
           disk_free_gb=30, sync_period_ms=MIN, cost_weight=0.5)
PC = Node(id="office", cpu=12, ram_mb=30000, ram_budget_mb=8000, resident_mb=600,
          disk_free_gb=638, gpu_vram_mb=6144, sync_period_ms=30 * MIN,
          reach=("domestic", "proxy"), cost_weight=1.0)

FRESH = Health(heartbeat_age_ms=10_000, freshness_ms=60_000)
OLD = Health(heartbeat_age_ms=10_000, freshness_ms=3 * HOUR)
GONE = Health(heartbeat_age_ms=30 * MIN, freshness_ms=HOUR)


def _t(**kw) -> Task:
    base = dict(kind="x", ram_mb=100, est_ms=5000, staleness_max_ms=HOUR)
    base.update(kw)
    return Task(**base)


# ── 状态机 ──────────────────────────────────────────────────
def test_心跳丢了就是_down() -> None:
    assert phase_of(GONE) is Phase.DOWN


def test_活着但数据旧就是_stale() -> None:
    assert phase_of(OLD) is Phase.STALE


def test_忙和空闲区分开() -> None:
    assert phase_of(Health(1000, 1000, busy=True)) is Phase.BUSY
    assert phase_of(Health(1000, 1000)) is Phase.IDLE


def test_要走了只收尾() -> None:
    assert phase_of(Health(1000, 1000, busy=True, draining=True)) is Phase.DRAINING


# ── 三条硬约束 ──────────────────────────────────────────────
def test_内存超预算就不派() -> None:
    ok, why = eligible(ECS, _t(ram_mb=400), FRESH)
    assert not ok and "预算" in why


def test_显存不够就不派() -> None:
    ok, why = eligible(ECS, _t(needs_gpu_mb=2048), FRESH)
    assert not ok and "显存" in why
    ok2, _ = eligible(PC, _t(needs_gpu_mb=2048), FRESH)
    assert ok2


def test_数据比同步周期还旧就不派() -> None:
    """任务只能忍 5 分钟，而工作机 30 分钟才同步一次——派过去就是拿着旧数据算。"""
    ok, why = eligible(PC, _t(staleness_max_ms=5 * MIN), FRESH)
    assert not ok and "同步" in why


def test_网络不可达就不派() -> None:
    ok, why = eligible(ECS, _t(needs_net=("proxy",)), FRESH)
    assert not ok and "网络" in why


# ── 放置 ────────────────────────────────────────────────────
def test_重活本能地落到工作机() -> None:
    heavy = _t(kind="consolidate", ram_mb=2000, est_ms=600_000,
               staleness_max_ms=6 * HOUR, needs_gpu_mb=2048)
    d = place(heavy, [ECS, PC], {"cloud": FRESH, "office": FRESH})
    assert d.ok and d.node == "office", d.reason


def test_轻活延迟敏感时落在云上() -> None:
    light = _t(kind="route", ram_mb=20, est_ms=50, staleness_max_ms=10 * MIN)
    d = place(light, [ECS, PC], {"cloud": FRESH, "office": FRESH})
    assert d.ok and d.node == "cloud", d.reason


def test_工作机不在时退回云上_而不是干等() -> None:
    """能在云上跑的就退回云上；跑不动的（比如 2 GB）就该等，不能硬塞。"""
    fits = _t(kind="dream-light", ram_mb=150, staleness_max_ms=6 * HOUR)
    d = place(fits, [ECS, PC], {"cloud": FRESH, "office": GONE})
    assert d.ok and d.node == "cloud", d.reason

    too_big = _t(kind="consolidate", ram_mb=2000, staleness_max_ms=6 * HOUR)
    d2 = place(too_big, [ECS, PC], {"cloud": FRESH, "office": GONE})
    assert not d2.ok and "预算" in d2.reason


def test_没有可用节点时必须说清楚为什么() -> None:
    d = place(_t(ram_mb=9000), [ECS, PC], {"cloud": FRESH, "office": GONE})
    assert not d.ok
    assert "预算" in d.reason and "心跳" in d.reason


# ── 让路 ────────────────────────────────────────────────────
def test_你在用机器就让路() -> None:
    assert should_preempt(_t(), FRESH, user_active=True)
    assert not should_preempt(_t(), FRESH, user_active=False)


def test_不可抢占的任务不随便让路() -> None:
    assert not should_preempt(_t(preemptible=False), FRESH, user_active=True)
