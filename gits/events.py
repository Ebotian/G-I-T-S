"""日志集的读写、导出/导入与重放游标。

同步协议就是重放协议（DESIGN §15.8）：同步的是**日志集**，不是 events 一张表。
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator

from .db import DB, LOG_STREAMS, Writer


def iter_events(db: DB, after_id: int = 0) -> Iterator[dict]:
    for r in db.conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id", (after_id,)):
        yield dict(r)


def iter_stream(db: DB, stream: str, after_id: int = 0) -> Iterator[dict]:
    if stream not in LOG_STREAMS:
        raise ValueError(f"未知的流: {stream}")
    pk = "event_id" if stream == "redactions" else (
        "id" if stream in ("events", "model_calls") else "ts_start"
    )
    for r in db.conn.execute(f"SELECT * FROM {stream} WHERE {pk} > ? ORDER BY {pk}", (after_id,)):
        yield dict(r)


def export_logset(db: DB, after: dict[str, int] | None = None) -> dict:
    """把增量日志集导出成可直接灌进空库的字节（JSON，P0 够用）。

    真实部署里换 zstd + CBOR，协议形状不变。
    """
    after = after or {}
    out: dict[str, list[dict]] = {}
    for stream in LOG_STREAMS:
        out[stream] = list(iter_stream(db, stream, after.get(stream, 0)))
    return out


def import_logset(db: DB, payload: dict, *, writer: Writer | None = None) -> dict[str, int]:
    """把日志集灌进一个空库。幂等：重复灌不会产生重复行。

    注意 redactions 要连带执行——否则重放会把删掉的内容造回来（§15.8.1）。
    """
    w = writer or Writer(db)
    counts: dict[str, int] = {}
    for row in payload.get("events", []):
        raw = row["payload"]
        if isinstance(raw, str):          # JSON 传输会把 bytes 变 str
            raw = raw.encode()
        _, created = w.append_event(
            source=row["source"], kind=row["kind"], ext_id=row["ext_id"],
            ts=row["ts"], recv_ts=row["recv_ts"], node_id=row["node_id"],
            payload=raw,
        )
        counts["events"] = counts.get("events", 0) + (1 if created else 0)

    with w.txn() as c:
        for row in payload.get("model_calls", []):
            req = row["request"]
            if isinstance(req, str):
                req = req.encode()
            c.execute(
                "INSERT INTO model_calls(id, ts, kind, model, prompt_hash, request,"
                " response, usage_json, event_id) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO NOTHING",
                (row["id"], row["ts"], row["kind"], row["model"], row["prompt_hash"],
                 req, row["response"], row["usage_json"], row["event_id"]),
            )
            counts["model_calls"] = counts.get("model_calls", 0) + 1

        for row in payload.get("observability", []):
            c.execute(
                "INSERT INTO observability(channel, ts_start, ts_end, state)"
                " VALUES(?,?,?,?) ON CONFLICT(channel, ts_start) DO NOTHING",
                (row["channel"], row["ts_start"], row["ts_end"], row["state"]),
            )
            counts["observability"] = counts.get("observability", 0) + 1

        for row in payload.get("redactions", []):
            c.execute(
                "INSERT INTO redactions(event_id, ts) VALUES(?,?)"
                " ON CONFLICT(event_id) DO NOTHING",
                (row["event_id"], row["ts"]),
            )
            c.execute("UPDATE events SET payload=NULL WHERE id=?", (row["event_id"],))
            counts["redactions"] = counts.get("redactions", 0) + 1

    return counts


def cursor(db: DB, node_id: str) -> dict[str, int]:
    return {
        r["stream"]: r["last_id"]
        for r in db.q("SELECT stream, last_id FROM sync_cursor WHERE node_id=?", (node_id,))
    }


def set_cursor(db: DB, node_id: str, stream: str, last_id: int) -> None:
    with db.conn:
        db.conn.execute(
            "INSERT INTO sync_cursor(node_id, stream, last_id, last_sync_ok)"
            " VALUES(?,?,?,strftime('%s','now')*1000)"
            " ON CONFLICT(node_id, stream) DO UPDATE SET"
            " last_id=excluded.last_id, last_sync_ok=excluded.last_sync_ok",
            (node_id, stream, last_id),
        )
