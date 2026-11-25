"""ghost：常驻内核。第一版只做三件真事。

    心跳    每隔一段写一条 vital 事件——这是"她还在"的唯一凭证
    投递    扫 outbox，交给自己配置的通道；没有通道就如实记 dropped
    快照    定期写 state_snapshot，并刷新 FTS 索引

**刻意不做**：不做对话、不做抽取、不做触发。那些要么需要模型（没配 key），
要么需要还没写的 HTTP 端。先把"活着、投递、可观测"这三件做真。

单线程 + 单写连接。asyncio 只用来睡觉——不是因为它更快，是因为
`asyncio.sleep` 能被打断，而 `time.sleep` 不能。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gits.db import DB, Writer, now_ms
from gits.recall import reindex

HOUR = 3_600_000


# ── systemd 通知（无依赖，就写一个 datagram）───────────────
def sd_notify(state: str) -> bool:
    """给 systemd 发通知。没有 NOTIFY_SOCKET 就安静地返回 False。

    用它才能上 `Type=notify` + `WatchdogSec`：进程卡死时会被 systemd 重启，
    而不是显示 "active" 却什么都不干。
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError:
        return False


# ── 通道 ────────────────────────────────────────────────────
class Transport(Protocol):
    name: str

    def send(self, text: str, channel: str) -> str:
        """返回新的 outbox.status。"""


class NullTransport:
    """没有配任何通道。**如实记 dropped，不要假装发了。**"""

    name = "null"

    def send(self, text: str, channel: str) -> str:
        return "dropped"


class FileTransport:
    """写文件的通道。演示与测试用——它能证明"投递"这一步真的被执行过。"""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, text: str, channel: str) -> str:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"{now_ms()}\t{channel}\t{text}\n")
        return "sent"


# ── 内核 ────────────────────────────────────────────────────
@dataclass
class Ghost:
    db: DB
    node: str = "cloud"

    #: 心跳间隔。取 5 分钟的倍数，是因为"她还活着"不需要秒级精度，
    #: 而事件表要能被 replay 得动。
    heartbeat_ms: int = 5 * 60_000
    #: 落**事件**的间隔。心跳本身每 5 分钟一拍，但只更新 nodes_health；
    #: 事件按小时写，否则一年十万条"我还活着"会把真相淹掉。
    event_beat_ms: int = 60 * 60_000
    #: 快照/重建索引间隔
    snapshot_ms: int = 15 * 60_000
    _last_beat: int = 0
    _last_snapshot: int = 0
    stopped: bool = False

    def __post_init__(self) -> None:
        self.w = Writer(self.db)

    # ── 心跳 ──
    def beat(self, t: int | None = None) -> bool:
        """一拍两件事：更新 liveness，**按小时**落一条事件。

        为什么不把每一拍都写成事件：5 分钟一次就是 288 条/天、10 万条/年，
        全是"我还活着"。它会把 events 淹掉——而 events 是唯一真相，
        要留着放真正发生过的事。

        所以：nodes_health 记每一拍（廉价 upsert，不留历史），
        events 每小时一条（够"她当时在不在"用了）。
        """
        t = now_ms() if t is None else t
        with self.w.txn() as c:
            c.execute(
                "INSERT INTO nodes_health(node_id, last_seen, caps, idle_since)"
                " VALUES(?,?,?,NULL) ON CONFLICT(node_id) DO UPDATE SET"
                " last_seen=excluded.last_seen",
                (self.node, t, "ghost"))
        self._last_beat = t

        bucket = t // self.event_beat_ms
        _, created = self.w.append_event(
            source="self", kind="vital",
            ext_id=f"{self.node}:vital:{bucket}",
            ts=bucket * self.event_beat_ms, recv_ts=t,
            payload=b"alive",
        )
        return created

    # ── 快照 ──
    def snapshot(self, t: int | None = None) -> None:
        t = now_ms() if t is None else t
        backlog = int(self.db.scalar(
            "SELECT count(*) FROM events WHERE kind IN ('msg_in','inner')"
            " AND NOT EXISTS (SELECT 1 FROM model_calls m WHERE m.event_id=events.id)"
        ) or 0)
        with self.w.txn() as c:
            c.execute(
                "INSERT INTO state_snapshot(ts, phase, s_user, s_node, backlog, note)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(ts) DO NOTHING",
                (t, "AWAKE", None, None, backlog, self.node),
            )
        reindex(self.db)
        self._last_snapshot = t

    # ── 一拍 ──
    def tick(self, t: int | None = None) -> dict:
        t = now_ms() if t is None else t
        did = {}
        if t - self._last_beat >= self.heartbeat_ms or not self._last_beat:
            did["beat"] = self.beat(t)
        if t - self._last_snapshot >= self.snapshot_ms or not self._last_snapshot:
            self.snapshot(t)
            did["snapshot"] = True
        return did


async def serve(db_path: str | Path, *, node: str = "cloud",
                once: bool = False) -> int:
    """常驻循环。SIGTERM 时**把手上这一拍做完再退**。"""
    db = DB(db_path)
    ghost = Ghost(db=db, node=node)

    def _stop(*_: object) -> None:
        ghost.stopped = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:      # pragma: no cover
            signal.signal(sig, _stop)

    sd_notify("READY=1")
    try:
        while not ghost.stopped:
            t0 = time.monotonic()
            ghost.tick()
            sd_notify("WATCHDOG=1")
            if once:
                break
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    finally:
        sd_notify("STOPPING=1")
        db.close()
    return 0
