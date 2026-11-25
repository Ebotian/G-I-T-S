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
    #: 回复间隔。每拍最多回一条——一次调用要几秒，
    #: 不能让它把心跳和快照挤掉。
    reply_ms: int = 1_000
    _last_reply: int = 0
    #: 抽取是后台活，没人在等——放慢一点，别和回话抢 timeout。
    extract_ms: int = 2_000
    _last_extract: int = 0
    #: 窗口决策。慢一点没关系——它只在积压时才有活干。
    ctx_ms: int = 60_000
    _last_ctx: int = 0
    #: 整理一次大概要多久。先拍 20 秒，之后用实测中位数替换。
    compact_ms_est: int = 20_000
    _compact_future: object | None = None
    _pool: object | None = None
    #: 人格更新。**默认只判定、不动作**（GITS_PERSONA_APPLY=1 才真写）——
    #: 这种行为一旦判错，第二天才发现，而那时候已经不可逆了。
    persona_apply: bool = False
    _last_night: int = 0
    _last_beat: int = 0
    _last_snapshot: int = 0
    stopped: bool = False
    last_error: str = ""

    def __post_init__(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.w = Writer(self.db)
        # 整理跑在这里，不占心跳线程（一次 thinking=high 要几秒到几十秒）
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="compact")

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
        if t - self._last_reply >= self.reply_ms:
            got = self.reply(t)
            if got:
                did["reply"] = got
        if t - self._last_extract >= self.extract_ms:
            got = self.extract(t)
            if got:
                did["extract"] = got
        if t - self._last_ctx >= self.ctx_ms:
            got = self.window(t)
            if got:
                did["ctx"] = got
        if t - self._last_night >= self.ctx_ms:
            got = self.night(t)
            if got:
                did["night"] = got
        return did

    def night(self, t: int | None = None) -> str:
        """夜里该不该动人格。先判定、记账；确认过得去了才真的改。"""
        from gits import night as N
        from gits import persona

        t = now_ms() if t is None else t
        self._last_night = t
        chk = N.check(self.db, t)
        if not chk.ok:
            return ""
        if not N.enough_time(chk, self.compact_ms_est * 2):
            return ""                       # 安静，但不够干完
        if not self.persona_apply:
            return f"（只判定）{chk.why}"
        try:
            got = persona.snapshot(self.db, now=t)
        except Exception as exc:
            self.last_error = f"persona: {type(exc).__name__}: {exc}"
            return "persona 失败"
        if got is None:
            return f"{chk.why}；但自我域里没有可选的东西，不更新"
        return f"{chk.why} → 第 {len(got.picked)} 个节点写成 v{got.event_id}"

    def window(self, t: int | None = None) -> str:
        """该不该换窗。**整理是后台活，先在闲得够久的时候做。**"""
        from gits import ctxwin
        from gits.compact import compact
        from gits.idle import estimate

        t = now_ms() if t is None else t
        self._last_ctx = t
        plan = ctxwin.plan(self.db, now=t, idle=estimate(self.db, t),
                           compact_ms=self.compact_ms_est)
        if plan.action in ("compact_idle", "compact_force"):
            # **整理不在心跳线程里跑。** 它要一次 thinking=high 的调用（几秒到几十秒），
            # 放在 tick 里等于这段时间他不回话。
            if self._compact_future is not None and not self._compact_future.done():
                return ""
            self._compact_future = self._pool.submit(
                compact, self.db, now=t, reason=plan.reason)
            return f"{plan.action} 已交给后台"
        elif plan.action == "new_window":
            ctxwin.new_window(self.db, ts=t, reason=plan.reason)
            return "new_window"
        return ""

    def extract(self, t: int | None = None) -> int:
        """把还没抽过的消息抽成图上的节点。

        **先回话，再抽取**：回话时那条消息本身就在 C 段里，不缺它；
        而抽取开着 thinking，慢一点无所谓。
        """
        from gits.extract import extract, pending

        self._last_extract = now_ms() if t is None else t
        n = 0
        for ev in pending(self.db, limit=1):
            try:
                extract(self.db, ev)
                n += 1
            except Exception as exc:                 # 解析失败不能带走进程
                self.last_error = f"extract {ev['id']}: {type(exc).__name__}: {exc}"
        return n

    def reply(self, t: int | None = None) -> int:
        """该回的话回一句。失败**不重试到天荒地老**——记下来，下一拍再说。"""
        from gits.respond import respond_pending

        self._last_reply = now_ms() if t is None else t
        try:
            turns = respond_pending(self.db, limit=1)
        except Exception as exc:                     # 模型挂了不能带走进程
            self.last_error = f"{type(exc).__name__}: {exc}"
            return 0
        self.last_error = ""
        return len(turns)


async def serve(db_path: str | Path, *, node: str = "cloud",
                once: bool = False, persona_apply: bool = False) -> int:
    """常驻循环。SIGTERM 时**把手上这一拍做完再退**。"""
    db = DB(db_path)
    ghost = Ghost(db=db, node=node, persona_apply=persona_apply)

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
