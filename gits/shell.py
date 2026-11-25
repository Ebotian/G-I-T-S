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
                    self._link_reply(t)
        self._last_poll = t
        return n

    def _link_reply(self, t: int) -> None:
        """把这次回复挂到最近一条已发出的 outbox 上。

        没有这一步，`outcomes` 永远是空的——而 `influence` 的强化
        全靠它（§3.2：「她能学习的唯一途径」）。
        """
        row = self.db.one(
            "SELECT id FROM outbox WHERE status='sent' ORDER BY id DESC LIMIT 1")
        if row is None:
            return
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


async def serve(db_path: str | Path, *, out_file: str | Path, in_file: str | Path,
                node: str = "cloud", once: bool = False) -> int:
    db = DB(db_path)
    shell = Shell(db=db, channels=[FileChannel(out_file, in_file)], node=node)

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
        db.close()
    return 0
