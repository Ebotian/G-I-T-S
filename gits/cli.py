"""gits CLI —— P0 只要四个动词。

    gits status                 一眼看清：谁在线、积压多少、配额剩多少
    gits say "..."              记一条 msg_in（合成数据入口）
    gits replay --restore       清空投影，只用日志集重建
    gits trace <outbox_id>      一条回复用了哪版叙事、哪些记忆
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import DB, Writer, now_ms
from .events import cursor, export_logset, import_logset, iter_events
from .project import dump_projection, restore, trace

DEFAULT_DB = Path.home() / ".local/share/gits/gits.db"


def _open(args: argparse.Namespace) -> DB:
    p = Path(args.db)
    p.parent.mkdir(parents=True, exist_ok=True)
    return DB(p)


def cmd_status(args: argparse.Namespace) -> int:
    with _open(args) as db:
        n_events = db.scalar("SELECT count(*) FROM events") or 0
        n_calls = db.scalar("SELECT count(*) FROM model_calls") or 0
        n_nodes = db.scalar("SELECT count(*) FROM nodes") or 0
        n_out = db.scalar("SELECT count(*) FROM outbox") or 0
        last = db.one("SELECT id, recv_ts FROM events ORDER BY id DESC LIMIT 1")
        behind = db.scalar(
            "SELECT count(*) FROM events e WHERE e.kind IN ('msg_in','inner')"
            " AND NOT EXISTS (SELECT 1 FROM model_calls m WHERE m.event_id=e.id)"
        ) or 0
        print(
            f"events={n_events} model_calls={n_calls} nodes={n_nodes} outbox={n_out}\n"
            f"last_event={last['id'] if last else '-'} "
            f"待抽取(积压)={behind}"
        )
        cur = cursor(db, "cloud")
        if cur:
            print("sync_cursor=" + json.dumps(cur, ensure_ascii=False))
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    """合成数据入口：写一条 msg_in，并配一份"模型结果"。

    真实形态里这一步是 LLM 抽取；P0 用一个确定性桩，好让重放测试可重复。
    """
    with _open(args) as db:
        w = Writer(db)
        ts = now_ms()
        ev_id, created = w.append_event(
            source=args.source, kind="msg_in", ext_id=args.ext_id or f"cli:{ts}",
            ts=ts, recv_ts=ts, payload=args.text.encode(),
        )
        if created:
            w.append_model_call(
                kind="extract", model="stub:v0", event_id=ev_id,
                response=args.text.strip()[:60],
            )
        print(json.dumps({"event_id": ev_id, "created": created}, ensure_ascii=False))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    with _open(args) as db:
        n = restore(db)
        print(f"replayed {n} events -> {dump_projection(db).canonical()[:80]}...")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    with _open(args) as db:
        print(json.dumps(trace(db, args.outbox_id), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with _open(args) as db:
        json.dump(export_logset(db), sys.stdout, ensure_ascii=False)
        print()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    with _open(args) as db:
        payload = json.load(sys.stdin)
        print(json.dumps(import_logset(db, payload), ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gits", description="G-I-T-S P0")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("say")
    s.add_argument("text")
    s.add_argument("--source", default="wechat")
    s.add_argument("--ext-id", default=None)
    s.set_defaults(fn=cmd_say)

    r = sub.add_parser("replay", help="恢复重放：清空投影，只用日志集重建")
    r.add_argument("--restore", action="store_true", help="（唯一的模式，保留以便以后加 --experiment）")
    r.set_defaults(fn=cmd_replay)

    t = sub.add_parser("trace")
    t.add_argument("outbox_id", type=int)
    t.set_defaults(fn=cmd_trace)

    sub.add_parser("export").set_defaults(fn=cmd_export)
    sub.add_parser("import").set_defaults(fn=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
