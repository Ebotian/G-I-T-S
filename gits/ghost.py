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

from gits.core.db import DB, Writer, now_ms
from gits.memory.recall import reindex

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
    #: 主动开口的判定间隔。判定很便宜（几次查询），不用太密。
    initiative_ms: int = 20 * 60_000
    _last_initiative: int = 0
    #: 外面的世界（天气/位置）。**读数是事件**，装配只读日志，
    #: 所以这里只是采集，不是决策。取不到就跳过，不加时延。
    world_ms: int = 30 * 60_000
    _last_world: int = 0
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
        # 索引在内容提交时就更新了（core/project.py），这里不再全量重建。
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
        if t - self._last_initiative >= self.initiative_ms:
            got = self.initiative(t)
            if got:
                did["initiative"] = got
        got = self.character_weekly(t)
        if got:
            did["character"] = got
        if t - self._last_world >= self.world_ms:
            got = self.world(t)
            if got:
                did["world"] = got
        if t - self._last_night >= self.ctx_ms:
            got = self.night(t)
            if got:
                did["night"] = got
            drifted = self.drift(t)
            if drifted:
                did["drift"] = drifted
            # 正反馈的驱动量往前推一格。**只有这里写**，gate 只读。
            from gits.social.drive import update as drive_update

            drive_update(self.db, "her", now=t)
        return did

    def initiative(self, t: int | None = None) -> str:
        """该不该主动开口。**判定很便宜，真的开口才贵。**

        以前这条路径是断的：gate 有逻辑、respond 支持 proactive，
        但没有任何东西把它们连起来——决策机制齐了，没人按下按钮。
        """
        from gits.speech import initiative as I

        t = now_ms() if t is None else t
        self._last_initiative = t
        try:
            got = I.check(self.db, now=t)
        except Exception as exc:
            self.fault("initiative", exc, now=t)
            return ""
        if not got.get("allow"):
            return ""
        # **决定 → 入队。** 这一步以前是断的：只落一条 initiative 事件就返回，
        # 库里有"决定"、没有"待发消息"，而 spoken() 又把它计为说过。
        ev = I.record(self.db, now=t, reason=str(got.get("why") or ""),
                      candidate=got.get("candidate"))
        if ev is None:
            return ""
        try:
            from gits.speech import opening

            to = self._contact()
            if to is None:
                # 通道不认识收件人：决定留在日志里，压力照旧攒着。
                return ""
            out = opening.compose(self.db, initiative_event=ev,
                                  candidate=got.get("candidate"),
                                  channel=str(to.get("channel") or "weixin"),
                                  to=to.get("meta"), now=t)
        except Exception as exc:
            self.fault("opening", exc, now=t)
            return ""
        return "" if out is None else str(got.get("why") or "allow")

    def character_weekly(self, t: int | None = None) -> str:
        """周日夜里，让模型**总结性地**更新角色事实。

        两件事刻意分开：模型提议数值，**代码判定能不能收**。
        "模型说这周长高了 8 厘米"这种事，靠提示词是拦不住的。
        """
        from gits.memory import character as C

        t = now_ms() if t is None else t
        if not C.due_weekly(self.db, now=t):
            return ""
        import json

        cur = C.current(self.db)
        try:
            from gits.speech.llm import chat

            ask = json.dumps({"已有": cur, "本周编号": C.week_of(t)},
                             ensure_ascii=False)
            reply = chat(
                [{"role": "system", "content":
                  "你在更新一份关于自己的客观记录（身高、体重这类）。"
                  "只输出 JSON：键是第一层，值是数字或短字符串。"
                  "没有变化就输出 {}。不要解释、不要新增未列出的键。"},
                 {"role": "user", "content": ask}],
                model="deepseek-flash", max_tokens=200, thinking=False)
            got = json.loads(reply.text or "{}")
        except Exception as exc:
            self.fault("character", exc, now=t)
            return ""
        if not isinstance(got, dict):
            return ""
        changed = []
        for key, value in got.items():
            if key not in C.ALLOWED:
                continue
            # **只动周更的那些。** 血型、性别这类不会变的事实，
            # 被"总结"成别的不是生活变化，是错误。
            if key not in C.WEEKLY_KEYS:
                continue
            ok, why = C.bounded(self.db, key=key, value=value, now=t)
            if not ok:
                self.fault("character:" + str(key), ValueError(why), now=t)
                continue
            if C.record(self.db, key=str(key), value=value, src="dream",
                        now=t):
                changed.append(str(key))
        # **标记这一周跑过了。** 没有它 due_weekly 永远为真——
        # 同一周重复调模型、重复花钱，而且不报错。
        C.mark_week(self.db, now=t)
        return ",".join(changed)

    def _contact(self) -> dict | None:
        """主动开口发给谁。**没有联系人就不发**，不猜。"""
        row = self.db.one("SELECT * FROM contacts ORDER BY last_ts DESC LIMIT 1")
        if row is None:
            return None
        keys = row.keys()
        token = row["context_token"] if "context_token" in keys else None
        return {"channel": row["channel"] if "channel" in keys else "weixin",
                "meta": {"to": row["actor_id"], "context_token": token}}

    def fault(self, where: str, exc: BaseException, *, now: int) -> None:
        """**静默失败要显示。**

        以前这里写的是 `except Exception: pass`——天气挂了三天，
        系统表现得完全正常，只是从不出声。现在每次失败落一条事件
        （按小时去重，不会刷屏），`gits status` 会显示。
        """
        try:
            self.w.append_event(
                source="self", kind="fault",
                ext_id=f"fault:{where}:{now // 3_600_000}",
                ts=now, recv_ts=now,
                payload=(where + ": " + type(exc).__name__ + ": "
                         + str(exc))[:300].encode())
        except Exception:
            pass                    # 记失败本身不能再失败

    # ── 外面的世界 ──
    def world(self, t: int | None = None) -> str:
        """取一次外面的读数。**没有天气是正常状态**，不抛、不重试。"""
        from gits.world import amap

        t = now_ms() if t is None else t
        self._last_world = t
        try:
            key, adcode = amap.config()
        except RuntimeError:
            return ""                 # 没配就是没有，不是错误
        # **天气跟着他走**：有位置就用他的城市，配置只是兜底。
        from gits.world import loc

        adcode = loc.adcode(self.db, now=t) or adcode
        got = amap.maybe(self.db, now=t, key=key, adcode=adcode)
        # 预报是**记录**，不是读数：有效到明天，所以按小时取一次就够。
        from gits.world import forecast as fc

        if got:
            try:
                fc.record(self.db, fc.fetch(adcode, key=key), now=t)
            except Exception as exc:
                self.fault("forecast", exc, now=t)
        # 学出来的东西增量学：游标之后才算，重复调用不重复计数。
        from gits.world import places

        try:
            places.learn(self.db)
        except Exception as exc:
            self.fault("places", exc, now=t)
        return (got or {}).get("weather", "") if got else ""

    def night(self, t: int | None = None) -> str:
        """夜里该不该动人格。先判定、记账；确认过得去了才真的改。"""
        from gits.time import night as N
        from gits.memory import persona

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

    def drift(self, t: int | None = None) -> int:
        """注意力漂一天。**只漂非主个体**——她的变化必须来自真实互动。"""
        from itertools import combinations

        from gits.social import actors
        from gits.social import attention
        from gits.social import peer as P

        t = now_ms() if t is None else t
        names = actors.peers(self.db)          # 不含她
        n = 0
        for a, b in combinations(names, 2):
            ov = P.overlap(self.db, a, b)
            attention.step(self.db, pair=(a, b), now=t,
                           pressure=attention.overlap_pressure(ov))
            n += 1
        return n

    def window(self, t: int | None = None) -> str:
        """该不该换窗。**整理是后台活，先在闲得够久的时候做。**"""
        from gits.speech import ctxwin
        from gits.memory.compact import compact
        from gits.time import expected_idle

        t = now_ms() if t is None else t
        self._last_ctx = t
        plan = ctxwin.plan(self.db, now=t, idle=expected_idle(self.db, t),
                           compact_ms=ctxwin.compact_ms_from_history(self.db))
        if plan.action in ("compact_idle", "compact_force"):
            # **整理不在心跳线程里跑。** 它要一次 thinking=high 的调用（几秒到几十秒），
            # 放在 tick 里等于这段时间他不回话。
            if self._compact_future is not None:
                if not self._compact_future.done():
                    return ""
                exc = self._compact_future.exception()   # 不读就永远不知道它炸了
                if exc is not None:
                    self.last_error = f"compact: {type(exc).__name__}: {exc}"
            self._compact_future = self._pool.submit(
                _compact_off_thread, str(self.db.path), t, plan.reason)
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
        from gits.memory.extract import extract, pending

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
        from gits.speech.respond import respond_pending

        self._last_reply = now_ms() if t is None else t
        try:
            turns = respond_pending(self.db, limit=1)
        except Exception as exc:                     # 模型挂了不能带走进程
            self.last_error = f"{type(exc).__name__}: {exc}"
            return 0
        self.last_error = ""
        return len(turns)


def _compact_off_thread(db_path: str, now: int, reason: str) -> str:
    """在线程里**开自己的连接**。

    sqlite3 的连接默认绑定创建它的线程，把主线程的 DB 交给线程池会直接
    ProgrammingError——而且主循环只检查 future.done()，异常从此没人读。
    这里自己开一条：单写者的纪律靠 BEGIN IMMEDIATE + busy_timeout 保证，
    不靠"只能有一个连接"。
    """
    from gits.memory.compact import compact

    db = DB(db_path)
    try:
        got = compact(db, now=now, reason=reason)
        return "" if got is None else f"窗 {got.window_id}"
    finally:
        db.close()


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
