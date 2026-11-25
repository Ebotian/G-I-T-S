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

    ★ 事件与模型结果**在同一个事务里**。分两次提交的话，中间崩一次就会留下
    "有事件、没结果"的半成品；而重试用的是同一个 ext_id，会被幂等键挡掉，
    于是这条事件永远补不上结果，恢复重放直接失败（外部评审复现过）。
    """
    with _open(args) as db:
        w = Writer(db)
        ts = now_ms()
        ext_id = args.ext_id or f"cli:{ts}"
        with w.txn() as c:
            cur = c.execute(
                "INSERT INTO events(ts, recv_ts, source, kind, ext_id, payload, lag, ts_trust)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source, ext_id) DO NOTHING",
                (ts, ts, args.source, "msg_in", ext_id, args.text.encode(), 0, 1),
            )
            created = bool(cur.rowcount)
            row = c.execute("SELECT id FROM events WHERE source=? AND ext_id=?",
                            (args.source, ext_id)).fetchone()
            ev_id = int(row["id"])
            # ★ 不管是不是新建，都要保证结果存在。
            # 上次如果崩在"写完事件、还没写结果"之间，重试会走进 created=False，
            # 于是这条事件永远没有抽取结果——恢复重放会直接失败。
            c.execute(
                "INSERT INTO model_calls(ts, kind, model, response, event_id, producer)"
                " SELECT ?,?,?,?,?,? WHERE NOT EXISTS ("
                "   SELECT 1 FROM model_calls WHERE event_id=? AND kind='extract')",
                (now_ms(), "extract", "stub:v0", args.text.strip()[:60],
                 ev_id, "cloud/stub@v0", ev_id),
            )
            repaired = (not created) and c.execute(
                "SELECT changes()").fetchone()[0] > 0
        print(json.dumps({"event_id": ev_id, "created": created,
                          "repaired": bool(repaired)}, ensure_ascii=False))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    with _open(args) as db:
        n = restore(db)
        print(f"replayed {n} events -> {dump_projection(db).canonical()[:80]}...")
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    """额度账户。像电子账户一样，规则全部可见。"""
    from .quota import Account, Budget, allowance
    b = Budget(monthly_cny=args.monthly)
    a = allowance(b, args.actor)
    cap = a.cny * 5
    acct = Account(actor=args.actor, balance=args.balance)
    print(acct.statement(a.cny, accrue_cap_days=5))
    print(f"\n  给她看的一行：{acct.hint(a.cny)}")
    (Path(args.db).parent).mkdir(parents=True, exist_ok=True)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """她此刻能想起什么。走真实的 nodes / node_content / FTS / r。"""
    from .recall import recall
    with _open(args) as db:
        rows = recall(db, args.query, context=args.context or "",
                      now_ms=args.now or now_ms(), k=args.k)
        if not rows:
            print("（什么都没想起来）")
            return 0
        for r in rows:
            print(f"{r.score:5.3f}  r={r.r:4.2f}  {r.line()}")
    return 0


def cmd_turn(args: argparse.Namespace) -> int:
    """拼出一轮的 prompt（A/B/C）。用来看「她记得什么」到底长什么样。"""
    from .context import PersonaSeed, RelationSnapshot, TurnContext, assemble, bucket
    from .recall import lines, recall
    with _open(args) as db:
        t = args.now or now_ms()
        rows = recall(db, args.text, context=args.context or "", now_ms=t, k=args.k)
        prompt = assemble(
            PersonaSeed(narrative=args.narrative, version=1),
            RelationSnapshot(bucket=bucket(t), summary="（B 段先留空）",
                             hubs=(), phase="醒着"),
            TurnContext(stimulus=args.text, recalled=lines(rows),
                        quota_hint="额度[░░░░░░░░] 0%0.7/2.0元"),
        )
        print(prompt.text)
        b = prompt.budget()
        print(f"--- a={b['a']} b={b['b']} c={b['c']} total={b['total']} tokens"
              f" · a_hash={prompt.a_hash[:12]}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    with _open(args) as db:
        print(json.dumps(trace(db, args.outbox_id), ensure_ascii=False, indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """从另一份库拉增量。反复调用直到没有新增（断点续传就这么简单）。"""
    from .replica import pull
    with _open(args) as dst:
        with DB(Path(args.src)) as src:
            rounds, total = 0, {}
            while True:
                r = pull(src, dst, args.node, limit_rows=args.batch)
                rounds += 1
                for k, v in r.applied.items():
                    total[k] = total.get(k, 0) + v
                if not any(r.applied.values()) or rounds >= args.max_rounds:
                    break
            print(r.summary())
            if total:
                print("本轮合计:", json.dumps(total, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """副本重放之后，投影必须和源一致。"""
    from .replica import verify
    with DB(Path(args.src)) as src:
        with DB(Path(args.db)) as dst:
            ok, why = verify(src, dst)
    print(("✅ " if ok else "❌ ") + why)
    return 0 if ok else 1


def cmd_export(args: argparse.Namespace) -> int:
    """导出成 JSON。二进制字段已经过 base64——直接 dump bytes 会 TypeError。"""
    with _open(args) as db:
        json.dump(export_logset(db), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
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

    q = sub.add_parser("quota", help="额度账户（像电子账户一样可见）")
    q.add_argument("--actor", default="her")
    q.add_argument("--monthly", type=float, default=200.0)
    q.add_argument("--balance", type=float, default=0.0)
    q.set_defaults(fn=cmd_quota)

    sy = sub.add_parser("sync", help="从另一份库拉增量（单向、可断点续传）")
    sy.add_argument("--src", required=True)
    sy.add_argument("--node", default="replica")
    sy.add_argument("--batch", type=int, default=500)
    sy.add_argument("--max-rounds", type=int, default=50)
    sy.set_defaults(fn=cmd_sync)

    vf = sub.add_parser("verify", help="比较两份库重放后的投影")
    vf.add_argument("--src", required=True)
    vf.set_defaults(fn=cmd_verify)

    rc = sub.add_parser("recall", help="她现在能想起什么")
    rc.add_argument("query")
    rc.add_argument("--context", default="")
    rc.add_argument("--k", type=int, default=5)
    rc.add_argument("--now", type=int, default=0)
    rc.set_defaults(fn=cmd_recall)

    tn = sub.add_parser("turn", help="拼出一轮 prompt（A/B/C）")
    tn.add_argument("text")
    tn.add_argument("--context", default="")
    tn.add_argument("--narrative", default="我不太会哄人。但我记得住你说过的话。")
    tn.add_argument("--k", type=int, default=5)
    tn.add_argument("--now", type=int, default=0)
    tn.set_defaults(fn=cmd_turn)

    sub.add_parser("export").set_defaults(fn=cmd_export)
    sub.add_parser("import").set_defaults(fn=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
