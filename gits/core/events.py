"""日志集的读写、导出/导入与重放游标。

同步协议就是重放协议（DESIGN §15.8）：同步的是**日志集**，不是 events 一张表。

⚠️ 跨节点只能引用**稳定身份**（source, ext_id），不能引用本地自增 id。
两份独立写入的库，同一个事件在两边的数字 id 不同；照抄数字 id 会让
模型结果挂到别的事件上、删除指向别的记录——而且不会报错。
"""

from __future__ import annotations

import base64
import json
from typing import Iterator

from .db import DB, LOG_STREAMS, Writer

#: 二进制字段的传输编码。JSON 里没有 bytes，直接 dump 会 TypeError。
#: 用 base64 而不是 latin-1：后者在 JSON 转义上会出各种边角问题。
BINARY_FIELDS = ("payload", "request")


def _enc(value):
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode()}
    return value


def _dec(value):
    if isinstance(value, dict) and "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    if isinstance(value, str):          # 兼容早期按 latin-1 存的字符串
        return value.encode()
    return value


def event_key(source: str, ext_id: str) -> str:
    """跨节点的稳定身份。所有引用都用它，不用数字 id。"""
    return f"{source}\x1f{ext_id}"


def iter_events(db: DB, after_id: int = 0) -> Iterator[dict]:
    for r in db.conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id", (after_id,)):
        yield dict(r)


def _stream_pk(stream: str) -> str:
    if stream in ("events", "model_calls", "redactions"):
        return "id"
    return "ts_start"


def iter_stream(db: DB, stream: str, after_id: int = 0) -> Iterator[dict]:
    if stream not in LOG_STREAMS:
        raise ValueError(f"未知的流: {stream}")
    pk = _stream_pk(stream)
    for r in db.conn.execute(
            f"SELECT * FROM {stream} WHERE {pk} > ? ORDER BY {pk}", (after_id,)):
        yield dict(r)


def export_logset(db: DB, after: dict[str, int] | None = None) -> dict:
    """把增量日志集导出成可以直接灌进空库的 JSON。

    事件带 `payload` 的 base64；模型结果与删除记录**额外带 `event_ref`**
    （source + ext_id），导入时靠它重建本地引用。
    """
    after = after or {}
    out: dict[str, list[dict]] = {}
    keys = {
        row["id"]: event_key(row["source"], row["ext_id"])
        for row in db.conn.execute("SELECT id, source, ext_id FROM events")
    }

    for stream in LOG_STREAMS:
        rows = []
        for row in iter_stream(db, stream, after.get(stream, 0)):
            item = {k: _enc(v) for k, v in row.items()}
            if stream in ("model_calls", "redactions") and row.get("event_id"):
                item["event_ref"] = keys.get(row["event_id"])
            rows.append(item)
        out[stream] = rows
    return out


def import_logset(db: DB, payload: dict, *, writer: Writer | None = None) -> dict[str, int]:
    """把日志集灌进一个库。幂等，且**按稳定身份重建引用**。

    两遍：
      1. 先落事件，建 (source, ext_id) → 本地 id 的映射
      2. 再用 `event_ref` 把模型结果和删除记录指到正确的事件上

    只做第一遍就会出这种事：对面删的是第 5 条，本地第 5 条是另一件事。
    """
    w = writer or Writer(db)
    counts: dict[str, int] = {}

    for row in payload.get("events", []):
        _, created = w.append_event(
            source=row["source"], kind=row["kind"], ext_id=row["ext_id"],
            ts=row["ts"], recv_ts=row["recv_ts"], node_id=row["node_id"],
            payload=_dec(row.get("payload")),
        )
        counts["events"] = counts.get("events", 0) + (1 if created else 0)

    # 第一遍结束：本地映射
    local = {
        event_key(r["source"], r["ext_id"]): r["id"]
        for r in db.conn.execute("SELECT id, source, ext_id FROM events")
    }

    with w.txn() as c:
        for row in payload.get("model_calls", []):
            ref = row.get("event_ref")
            event_id = local.get(ref) if ref else row.get("event_id")
            c.execute(
                "INSERT INTO model_calls(id, ts, kind, model, prompt_hash, request,"
                " response, usage_json, event_id, producer)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (row["id"], row["ts"], row["kind"], row["model"], row["prompt_hash"],
                 _dec(row.get("request")), row["response"], row["usage_json"],
                 event_id, row.get("producer")),
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
            ref = row.get("event_ref")
            event_id = local.get(ref) if ref else row.get("event_id")
            if event_id is None:
                continue
            c.execute(
                "INSERT INTO redactions(event_id, ts) VALUES(?,?)"
                " ON CONFLICT(event_id) DO NOTHING",
                (event_id, row["ts"]),
            )
            # 删除要连模型结果一起清，否则重放会把正文从抽取响应里读回来
            c.execute("UPDATE events SET payload=NULL WHERE id=?", (event_id,))
            c.execute("UPDATE model_calls SET response=NULL, request=NULL"
                      " WHERE event_id=?", (event_id,))
            counts["redactions"] = counts.get("redactions", 0) + 1

    return counts


def cursor(db: DB, node_id: str) -> dict[str, int]:
    return {
        r["stream"]: r["last_id"]
        for r in db.q("SELECT stream, last_id FROM sync_cursor WHERE node_id=?",
                      (node_id,))
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
