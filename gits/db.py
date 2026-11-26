"""连接、迁移与唯一写者。

P0 的形态（DESIGN §16 / impl §4.1）：
    一个进程 · 一个库 · 一个写连接
后台进程"提交结果"，不写库；写连接由 ghost 永久持有。

提交语义（impl §4.2）：**先 commit（fsync），再 ACK**。
所以 append_* 返回之时，数据已经在盘上。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_DIR = Path(__file__).parent / "schema"

#: 真相所在的流。同步与重放都以它们为单位（DESIGN §3.1）。
LOG_STREAMS = ("events", "model_calls", "observability", "redactions")

#: 由日志集派生的表。任何时刻都可以整表删除并重建。
PROJECTION_TABLES = ("nodes", "node_content", "edges", "outbox", "influence",
                     "facts", "entity_alias")


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    """一个连接。读随便读，写只经由 :class:`Writer`。"""

    def __init__(self, path: str | Path, *, durable: bool = True) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # events 库必须 FULL：断电最多丢最后一秒（DESIGN §11.8-5）
        self.conn.execute("PRAGMA synchronous=" + ("FULL" if durable else "NORMAL"))
        self._migrate()

    # ── 迁移 ───────────────────────────────────────────────
    def _migrate(self) -> None:
        """按文件名编号顺序应用，`user_version` 记已应用到哪。

        派生表永远可以整库重建（schema §6），所以迁移写坏了有退路。
        """
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        for path in sorted(SCHEMA_DIR.glob("[0-9]*_*.sql")):
            n = int(path.name.split("_", 1)[0])
            if n <= version:
                continue
            sql = path.read_text(encoding="utf-8")
            self.conn.executescript(
                f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={n};\nCOMMIT;"
            )
            version = n

    # ── 便捷读 ─────────────────────────────────────────────
    def q(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, args: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, args).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Writer:
    """唯一写者。

    这里刻意不用 asyncio.Queue——它只在进程内有效（第二轮评审抓到的错）。
    P0 就一个连接、一把锁，谁也抢不到第二个写连接。
    """

    def __init__(self, db: DB) -> None:
        self.db = db
        self.conn = db.conn

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")   # ← 返回即 fsync 完成，之后才可以 ACK

    # ── 日志集 ─────────────────────────────────────────────
    def append_event(
        self,
        *,
        source: str,
        kind: str,
        ext_id: str,
        ts: int | None = None,
        recv_ts: int | None = None,
        node_id: str | None = None,
        payload: bytes | None = None,
        scope: str = "world",
    ) -> tuple[int, bool]:
        """追加一条事件。返回 (id, created)。

        幂等键是 (source, ext_id)。重复投递返回已有 id，created=False。
        **绝不抛"已存在"异常**——"至少一次投递"是常态，不是错误。
        """
        if not ext_id:
            raise ValueError("ext_id 必须非空：可空的幂等键等于没有幂等键")
        ts = now_ms() if ts is None else ts
        recv_ts = now_ms() if recv_ts is None else recv_ts
        lag = recv_ts - ts
        # 上传延迟超过 60s 就认为该事件的 ts 不可用于节律训练
        ts_trust = 1 if lag < 60_000 else 0
        with self.txn() as c:
            cur = c.execute(
                "INSERT INTO events(ts, recv_ts, source, kind, ext_id, node_id,"
                " payload, lag, ts_trust, scope) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source, ext_id) DO NOTHING",
                (ts, recv_ts, source, kind, ext_id, node_id, payload, lag, ts_trust,
                 scope),
            )
            if cur.rowcount:
                return int(cur.lastrowid), True
        row = self.db.one(
            "SELECT id FROM events WHERE source=? AND ext_id=?", (source, ext_id)
        )
        assert row is not None
        return int(row["id"]), False

    def append_events_bulk(self, rows: Sequence[dict]) -> int:
        """批量追加（合成数据与回填用）。一个事务，避免逐条 fsync。

        仍然是同一个写连接——批量不等于并发。
        """
        n = 0
        with self.txn() as c:
            for r in rows:
                if not r.get("ext_id"):
                    raise ValueError("ext_id 必须非空")
                ts = int(r["ts"])
                recv_ts = int(r.get("recv_ts", ts))
                cur = c.execute(
                    "INSERT INTO events(ts, recv_ts, source, kind, ext_id, node_id,"
                    " payload, lag, ts_trust) VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(source, ext_id) DO NOTHING",
                    (ts, recv_ts, r["source"], r["kind"], r["ext_id"],
                     r.get("node_id"), r.get("payload"), recv_ts - ts,
                     1 if recv_ts - ts < 60_000 else 0),
                )
                n += cur.rowcount
        return n

    def append_model_call(
        self,
        *,
        kind: str,
        model: str,
        response: str,
        event_id: int | None = None,
        prompt_hash: str | None = None,
        request: bytes | None = None,
        usage_json: str | None = None,
        meta: str | None = None,
        ts: int | None = None,
    ) -> int:
        with self.txn() as c:
            cur = c.execute(
                "INSERT INTO model_calls(ts, kind, model, prompt_hash, request,"
                " response, usage_json, event_id, meta)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    now_ms() if ts is None else ts,
                    kind, model, prompt_hash, request, response, usage_json,
                    event_id, meta,
                ),
            )
            return int(cur.lastrowid)

    def set_observability(self, channel: str, ts_start: int, state: str,
                          ts_end: int | None = None) -> None:
        with self.txn() as c:
            c.execute(
                "INSERT INTO observability(channel, ts_start, ts_end, state)"
                " VALUES(?,?,?,?) ON CONFLICT(channel, ts_start)"
                " DO UPDATE SET state=excluded.state, ts_end=excluded.ts_end",
                (channel, ts_start, ts_end, state),
            )

    def redact_event(self, event_id: int) -> None:
        """删除 = 一条事件 + 清 payload **和模型结果**（DESIGN §15.8.1）。

        只清 payload 是不够的：抽取响应里同样有正文，重放会把它读出来
        重新造出节点。所以两边一起清。
        """
        ts = now_ms()
        with self.txn() as c:
            c.execute("UPDATE events SET payload=NULL WHERE id=?", (event_id,))
            c.execute("UPDATE model_calls SET response=NULL, request=NULL"
                      " WHERE event_id=?", (event_id,))
            c.execute(
                "INSERT INTO redactions(event_id, ts) VALUES(?,?)"
                " ON CONFLICT(event_id) DO NOTHING",
                (event_id, ts),
            )
            c.execute(
                "INSERT INTO events(ts, recv_ts, source, kind, ext_id, payload, lag, ts_trust)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source, ext_id) DO NOTHING",
                (ts, ts, "self", "redact", f"redact:{event_id}", None, 0, 1),
            )
