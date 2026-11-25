"""多端调度：把"在哪台机器上算"变成可判定的问题。

三条硬约束，正好对应三件容易各说各话的事（DESIGN §19）：

  1. **能力**：任务的内存峰值 + 节点已常驻的内存 ≤ 节点预算
  2. **新鲜度**：任务能容忍的陈旧上限 ≥ 节点同步一次需要的时间
  3. **时机**：节点此刻不在 down / draining，且队列没满

只要有一条不满足就**不派**，而不是"先派过去试试"——在没人看着的后台，
"试试"的结果通常是跑到一半被 OOM 杀掉，然后第二天你看到一条半成品。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

HOUR = 3_600_000


class Phase(str, Enum):
    """节点此刻的状态。心跳、空闲、租约三者决定，模型不参与。"""

    DOWN = "down"            # 心跳丢了
    STALE = "stale"          # 活着但数据太旧
    IDLE = "idle"            # 可以接活
    BUSY = "busy"            # 手上已有任务
    DRAINING = "draining"    # 要走了，只收尾不接新活


@dataclass(frozen=True)
class Node:
    """一台机器的画像。数字都是**实测**的，不是想当然。"""

    id: str
    cpu: int
    ram_mb: int
    ram_budget_mb: int          # 允许后台作业占用的上限
    resident_mb: int            # 常驻进程已经吃掉的
    disk_free_gb: int
    gpu_vram_mb: int = 0
    sync_period_ms: int = HOUR  # 多久同步一次日志集
    reach: tuple[str, ...] = ("domestic",)
    cost_weight: float = 1.0    # 越小越优先（电费/机时）

    def headroom_mb(self) -> int:
        return self.ram_budget_mb - self.resident_mb


@dataclass(frozen=True)
class Task:
    """一个作业的画像。"""

    kind: str
    ram_mb: int
    est_ms: int
    staleness_max_ms: int
    needs_gpu_mb: int = 0
    needs_net: tuple[str, ...] = ()
    preemptible: bool = True


@dataclass(frozen=True)
class Health:
    heartbeat_age_ms: int
    freshness_ms: int
    busy: bool = False
    draining: bool = False


@dataclass(frozen=True)
class Decision:
    node: str | None
    reason: str
    phase: str = ""

    @property
    def ok(self) -> bool:
        return self.node is not None


def phase_of(h: Health, *, beat_timeout_ms: int = 3 * 60_000,
             stale_after_ms: int = 2 * HOUR) -> Phase:
    if h.heartbeat_age_ms > beat_timeout_ms:
        return Phase.DOWN
    if h.draining:
        return Phase.DRAINING
    if h.freshness_ms > stale_after_ms:
        return Phase.STALE
    return Phase.BUSY if h.busy else Phase.IDLE


def eligible(node: Node, task: Task, h: Health) -> tuple[bool, str]:
    """三条硬约束。返回 (能不能, 为什么)。"""
    ph = phase_of(h)
    if ph is Phase.DOWN:
        return False, "心跳丢了"
    if ph is Phase.DRAINING:
        return False, "正在收尾，不接新活"

    if task.needs_gpu_mb and node.gpu_vram_mb < task.needs_gpu_mb:
        return False, f"显存不够（要 {task.needs_gpu_mb}，有 {node.gpu_vram_mb}）"
    if task.ram_mb > node.headroom_mb():
        return False, f"内存超预算（要 {task.ram_mb}，余 {node.headroom_mb()}）"
    if task.staleness_max_ms < node.sync_period_ms:
        return False, (
            f"数据太旧（任务容忍 {task.staleness_max_ms // 60_000}min，"
            f"该节点 {node.sync_period_ms // 60_000}min 才同步一次）"
        )
    for net in task.needs_net:
        if net not in node.reach:
            return False, f"网络不可达（需要 {net}）"
    return True, "ok"


def place(task: Task, nodes: list[Node], healths: dict[str, Health]) -> Decision:
    """选一台机器。先过硬约束，再按"余量最大、成本最低、数据最新"排序。"""
    cands: list[tuple[float, Node, Health]] = []
    reasons: list[str] = []
    for n in nodes:
        h = healths.get(n.id)
        if h is None:
            reasons.append(f"{n.id}: 没有心跳")
            continue
        ok, why = eligible(n, task, h)
        if not ok:
            reasons.append(f"{n.id}: {why}")
            continue
        score = n.cost_weight * (1.0 + h.freshness_ms / HOUR)
        cands.append((score, n, h))

    if not cands:
        return Decision(None, "无处可派: " + "；".join(reasons))
    cands.sort(key=lambda x: (x[0], -x[1].headroom_mb()))
    _, best, h = cands[0]
    return Decision(best.id, f"派给 {best.id}（余量 {best.headroom_mb()} MB，"
                             f"数据 {h.freshness_ms // 60_000} 分钟旧）",
                    phase_of(h).value)


def should_preempt(task: Task, h: Health, *, user_active: bool) -> bool:
    """让路规则：你在用机器，就让。

    这条不是礼貌问题，是信任问题——后台作业卡你一次，你会永久关掉它。
    """
    if not task.preemptible:
        return False
    return user_active or phase_of(h) is Phase.DRAINING
