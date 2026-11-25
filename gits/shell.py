"""shell：通道。往外投递，往里收消息。

第一版只给一个 **文件通道**（叫它 loopback）：发出去写一行 JSONL，
收消息从一个投递箱文件读。它不需要网络、不需要模型、不需要第三方。

看起来像个玩具，但它能证明三件真事：

    outbox → 通道 → sent → outcomes     投递闭环
    投递箱 → msg_in 事件 → 下一轮能读到   收消息闭环
    回复挂到上一条已发送的 outbox 上       §3.2 的 outcomes 才有东西可学

换成微信/Mumble 时，只要实现同一个 `Channel` 协议。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gits.db import DB, Writer, now_ms
from gits.delivery import Dispatch, dispatch
from gits.ghost import sd_notify


class Channel(Protocol):
    name: str

    def deliver(self, row: dict) -> tuple[str, str]:
        """返回 (outbox.status, meta)。"""

    def poll(self) -> list[dict]:
        """返回收到的消息：[{text, ts, ext_id}]。"""


# ── 文件通道 ────────────────────────────────────────────────
class FileChannel:
    """发到 out_file，收自 in_file。两个文件都是 JSONL。

    `in_file` 的一行长这样（模拟"他回了一句"）：
        {"text": "在的", "ext_id": "m1"}
    """

    name = "file"
    #: 这个通道认哪些 channel 名。文件通道是调试用的兜底，认"没有专门通道"的那些。
    names = frozenset({"file", "desk_audio", "mumble"})

    def __init__(self, out_file: str | Path, in_file: str | Path) -> None:
        self.out = Path(out_file)
        self.inbox = Path(in_file)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self._offset = 0

    def deliver(self, row: dict) -> tuple[str, str]:
        with self.out.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": row["ts"], "channel": row["channel"],
                                "text": row["text"], "id": row["id"]},
                               ensure_ascii=False) + "\n")
        return "sent", self.name

    def poll(self) -> list[dict]:
        if not self.inbox.exists():
            return []
        size = self.inbox.stat().st_size
        if size <= self._offset:          # 文件被截断/重建
            self._offset = 0
        out = []
        with self.inbox.open("r", encoding="utf-8") as f:
            f.seek(self._offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append(item)
            self._offset = f.tell()
        return out


# ── shell ───────────────────────────────────────────────────
@dataclass
class Shell:
    db: DB
    channels: list[Channel] = field(default_factory=list)
    node: str = "cloud"
    scan_ms: int = 2_000
    poll_ms: int = 2_000

    _last_scan: int = 0
    _last_poll: int = 0
    stopped: bool = False

    def __post_init__(self) -> None:
        self.w = Writer(self.db)

    # ── 往外 ──
    def flush(self, t: int | None = None) -> Dispatch:
        t = now_ms() if t is None else t
        agg = Dispatch()
        for ch in self.channels:
            d = dispatch(self.db, ch, now=t)
            agg.sent += d.sent
            agg.failed += d.failed
            agg.expired += d.expired
            agg.retried += d.retried
        self._last_scan = t
        return agg

    # ── 往里 ──
    def intake(self, t: int | None = None) -> int:
        """把收到的消息写进 events。`ext_id` 保证同一条不会被算两遍。"""
        t = now_ms() if t is None else t
        n = 0
        for ch in self.channels:
            for item in ch.poll():
                ext = item.get("ext_id") or f"{ch.name}:{item.get('ts', t)}:{hash(item.get('text','')) & 0xffff}"
                _, created = self.w.append_event(
                    source="shell", kind="msg_in", ext_id=str(ext),
                    ts=int(item.get("ts", t)), recv_ts=t,
                    payload=str(item.get("text", "")).encode(),
                )
                if created:
                    n += 1
                    self._remember(item, ch.name, t)
                    self._link_reply(t)
        self._last_poll = t
        return n

    def _remember(self, item: dict, channel: str, t: int) -> None:
        """记住这条消息的 context_token。

        没有它，回复挂不到会话上——而它只在**收到消息的那一刻**存在，
        过时不候。所以收到就存，不等要发的时候再找。
        """
        actor = item.get("from") or item.get("actor_id")
        if not actor:
            return
        with self.w.txn() as c:
            c.execute(
                "INSERT INTO contacts(actor_id, channel, last_context_token,"
                " last_seen, last_text) VALUES(?,?,?,?,?)"
                " ON CONFLICT(actor_id) DO UPDATE SET"
                " last_context_token=excluded.last_context_token,"
                " last_seen=excluded.last_seen, last_text=excluded.last_text",
                (actor, channel, item.get("context_token"), t,
                 str(item.get("text", ""))[:200]),
            )

    def _link_reply(self, t: int) -> None:
        """把这次回复挂到**一条还没被回过的**已发送 outbox 上。

        没有这一步，`outcomes` 永远是空的——而 `influence` 的强化
        全靠它（§3.2：「她能学习的唯一途径」）。

        第一版取的是"最近一条 sent"，于是同一行被挂了两次 reply，
        而她其实只被回了一次。改成：**只挂还没有 reply 的那条**。
        """
        row = self.db.one(
            "SELECT o.id FROM outbox o WHERE o.status='sent'"
            " AND NOT EXISTS (SELECT 1 FROM outcomes x"
            "                 WHERE x.outbox_id=o.id AND x.kind='reply')"
            " ORDER BY o.id DESC LIMIT 1")
        if row is None:
            return                      # 没发过、或都回过了——不硬凑
        with self.w.txn() as c:
            c.execute("INSERT INTO outcomes(outbox_id, ts, kind, ref_event_id)"
                      " VALUES(?,?,?,NULL)", (row["id"], t, "reply"))

    # ── 一拍 ──
    def tick(self, t: int | None = None) -> dict:
        t = now_ms() if t is None else t
        did: dict = {}
        if t - self._last_scan >= self.scan_ms:
            d = self.flush(t)
            if d:
                did["flush"] = d.summary()
        if t - self._last_poll >= self.poll_ms:
            n = self.intake(t)
            if n:
                did["intake"] = n
        return did


class WeixinAdapter:
    """把微信通道包成 Channel：顺手启动/停止它的后台轮询线程。"""

    name = "weixin"
    names = frozenset({"weixin"})

    def __init__(self, account, *, cursor_path=None) -> None:
        from gits.channels.weixin import WeixinChannel

        self.inner = WeixinChannel(account, cursor_path=cursor_path)

    def start(self) -> None:
        self.inner.start()

    def stop(self) -> None:
        self.inner.stop()

    def deliver(self, row: dict) -> tuple[str, str]:
        return self.inner.deliver(row)

    def poll(self) -> list[dict]:
        return self.inner.poll()


async def serve(db_path: str | Path, *, channels: list | None = None,
                out_file: str | Path | None = None,
                in_file: str | Path | None = None,
                node: str = "cloud", once: bool = False) -> int:
    db = DB(db_path)
    chans = list(channels or [])
    if out_file and in_file:
        chans.append(FileChannel(out_file, in_file))
    shell = Shell(db=db, channels=chans, node=node)
    for c in chans:
        if hasattr(c, "start"):
            c.start()

    def _stop(*_: object) -> None:
        shell.stopped = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:          # pragma: no cover
            signal.signal(sig, _stop)

    sd_notify("READY=1")
    try:
        while not shell.stopped:
            t0 = time.monotonic()
            shell.tick()
            sd_notify("WATCHDOG=1")
            if once:
                break
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    finally:
        sd_notify("STOPPING=1")
        for c in shell.channels:
            if hasattr(c, "stop"):
                c.stop()
        db.close()
    return 0
