"""gits CLI —— P0 只要四个动词。

    gits status                 一眼看清：谁在线、积压多少、配额剩多少
    gits say "..."              记一条 msg_in（合成数据入口）
    gits replay --restore       清空投影，只用日志集重建
    gits trace <outbox_id>      一条回复用了哪版叙事、哪些记忆
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from gits.core.db import DB, Writer, now_ms
from gits.core.events import cursor, export_logset, import_logset, iter_events
from gits.core.project import dump_projection, restore, trace

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
        from gits.memory import persona
        from gits.core import wipe

        state = wipe.states(db)
        a = persona.current(db)
        print(
            f"events={n_events} model_calls={n_calls} nodes={n_nodes} outbox={n_out}\n"
            f"last_event={last['id'] if last else '-'} "
            f"待抽取(积压)={behind}\n"
            f"A 段={'空（不会说话）' if a is None else 'v' + str(a['version'])}\n"
            f"内容={json.dumps(state, ensure_ascii=False) if state else '{}（干净）'}"
        )
        # 缓存命中率是**上线后第一个要看的数**：它掉下去的时候，别的东西都还正常。
        import time as _time

        from gits.core import backup
        from gits.memory.extract import given_up
        from gits.speech import shape

        now = int(_time.time() * 1000)
        print(shape.line(db, now=now))
        # **失败要可见。**抽取失败以前只写进程内存，重启就没了；
        # 放弃的消息不会再有记忆，但也没有任何地方说过这件事。
        fails = db.scalar(
            "SELECT count(*) FROM model_calls WHERE kind='extract_failed'"
            " AND ts > ?", (now - 86_400_000,)) or 0
        dead = len(given_up(db))
        latest = backup.latest(backup.DEFAULT_DEST) or "（没有）"
        print(f"抽取失败(24h)={fails} 放弃={dead} 上次备份={latest}")
        # **静默失败要显示。** 后台那几个 except 以前是哑的：
        # 天气挂了三天，系统表现得完全正常，只是从不出声。
        faults = db.q("SELECT ts, payload FROM events WHERE kind='fault'"
                      " ORDER BY id DESC LIMIT 3")
        if faults:
            print("故障（最近三条）:")
            for f in faults:
                when = __import__("datetime").datetime.fromtimestamp(
                    int(f["ts"]) / 1000).strftime("%m-%d %H:%M")
                print(f"  {when}  {(f['payload'] or b'').decode('utf-8', 'replace')}")
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
    if args.shadow:
        return _replay_shadow(args)
    with _open(args) as db:
        n = restore(db)
        print(f"replayed {n} events -> {dump_projection(db).canonical()[:80]}...")
    return 0


def _delivery_view(db) -> dict:
    """"还欠谁一句话"的可比形式。**按稳定身份比，不按行号。**"""
    from gits.speech.respond import pending

    owed = sorted(f"{e['source']}:{e['ext_id']}" for e in pending(db, limit=200))
    replied = int(db.scalar(
        "SELECT count(*) FROM events WHERE kind='replied'") or 0)
    return {"pending": owed, "replied": replied}


def _replay_shadow(args: argparse.Namespace) -> int:
    """在**副本**上重放，对比线上投影。**不碰正在用的库。**

    为什么夜里不该对生产库直接 replay：`outbox` 和 `influence` 是投影表，
    重放会清空它们。回复本身在事件里（`replied` 标记），但如果一条回复
    已经进了 outbox 还没发出去，重放就把它丢了；更糟的是旧版靠 outbox 判断
    "回没回过"，重放之后所有旧消息会重新变成待回复——重答、重发、重复付费。

    所以：校验放影子库，生产库只在**真的需要恢复**时重建。
    """
    import shutil
    import sqlite3

    src = Path(args.db)
    shadow = src.with_suffix(src.suffix + ".shadow")
    for extra in ("", "-wal", "-shm"):
        p = Path(str(src) + extra)
        if p.exists():
            shutil.copy2(p, Path(str(shadow) + extra))
    try:
        live = DB(src)
        sdb = DB(shadow)
        n = restore(sdb)
        # **比"接下来该发什么"，不是比节点数。** 两边节点数一样、
        # 但对"还欠谁一句话"给出不同答案，才是会咬人的那种不一致。
        live_view = _delivery_view(live)
        shadow_view = _delivery_view(sdb)
        sdb.close()
        live.close()
        same = live_view == shadow_view
        print(f"影子重放 {n} 条事件：待回复 线上 {len(live_view['pending'])} / "
              f"重建 {len(shadow_view['pending'])}，已回复 {live_view['replied']} / "
              f"{shadow_view['replied']} → {'一致' if same else '**不一致**'}")
        if not same:
            only_live = sorted(set(live_view['pending']) - set(shadow_view['pending']))
            only_shadow = sorted(set(shadow_view['pending']) - set(live_view['pending']))
            if only_live:
                print(f"  只在线上待回复：{only_live[:5]}")
            if only_shadow:
                print(f"  只在重建里待回复：{only_shadow[:5]}")
        return 0 if same else 2
    finally:
        for extra in ("", "-wal", "-shm"):
            Path(str(shadow) + extra).unlink(missing_ok=True)


def cmd_loc(args: argparse.Namespace) -> int:
    """手机上报一次位置。**我们接收，不追踪。**

    手机端一行：
        termux-location -p gps | ssh gits@ecs gits loc --stdin
    坐标留本地，出上下文的是城市名。
    """
    import json
    import sys
    import time

    from gits.core.db import DB
    from gits.world import loc

    raw: dict = {}
    if args.stdin or args.lat is None:
        try:
            raw = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            print("  stdin 不是合法 JSON")
            return 2
    lat = args.lat if args.lat is not None else raw.get("latitude")
    lon = args.lon if args.lon is not None else raw.get("longitude")
    if lat is None or lon is None:
        print("  没有坐标")
        return 2
    acc = args.acc if args.acc is not None else raw.get("accuracy")
    source = args.source or ("gps" if raw.get("provider") in (None, "gps") else raw["provider"])

    now = int(time.time() * 1000)
    # 手机自己盖的章优先。termux-location 0.60 不返回定位时间，
    # 所以手机端要额外塞一个 phone_ts，否则时间戳只是收件时间。
    phone_ts = raw.get("phone_ts")
    fix_ts = int(phone_ts) if phone_ts else None
    elapsed = raw.get("elapsedMs")
    if elapsed is None:
        elapsed = raw.get("elapsed_ms")
    db = DB(Path(args.db))
    _cached = bool(raw.get("cached")) or (elapsed is not None and int(elapsed) < 50)
    city = None
    key = __import__("os").environ.get("GITS_AMAP_KEY")
    if key:
        city = loc.reverse(db, float(lat), float(lon), key=key)
    _ev, created = loc.record(db, lat=float(lat), lon=float(lon),
                              acc=float(acc) if acc is not None else None,
                              source=source, fix_ts=fix_ts, now=now, city=city,
                              elapsed_ms=int(elapsed) if elapsed is not None else None)
    where = ", ".join(filter(None, [(city or {}).get("province"),
                                    (city or {}).get("city"),
                                    (city or {}).get("district")])) or "（拿不到城市）"
    lag = (now - fix_ts) // 1000 if fix_ts else 0
    print(f"  {'收到' if created else '重复（已在账上）'}：{where}  精度 {acc or '?'} m  "
          f"来源 {source}  传输 {lag}s" + ("  （缓存定位）" if _cached else ""))
    return 0


def cmd_ingress(args: argparse.Namespace) -> int:
    """位置入口：唯一对公网开的那道门。只认 POST /loc。"""
    import os

    from gits.io import ingress

    secret = os.environ.get("GITS_INGRESS_SECRET") or ""
    if not secret:
        print("  没配 GITS_INGRESS_SECRET")
        return 2
    cert = os.environ.get("GITS_TLS_CERT") or None
    key = os.environ.get("GITS_TLS_KEY") or None
    if (cert is None) != (key is None):
        print("  GITS_TLS_CERT 和 GITS_TLS_KEY 要一起给")
        return 2
    print(f"  监听 :{args.port}  {'TLS' if cert else '明文（仅本地调试）'}  只认 POST /loc")
    ingress.serve(db_path=args.db, secret=secret, port=args.port, cert=cert, key=key)
    return 0


def cmd_relation(args) -> int:
    """记一次 / 看账本。**只能加，不能改，不能减。**"""
    from gits.memory import relations as R

    db = DB(args.db)
    now = int(time.time() * 1000)
    if not args.action:
        c = R.counters(db, now=now)
        if not c:
            print("  账本是空的")
        for k, v in sorted(c.items()):
            print(f"  {k:14s} {v}")
        db.close()
        return 0
    if args.action == "seed":
        ev = R.seed(db, now=now, body_count=args.count or 0)
        print(f"  前史记下了：body_count={args.count or 0}" if ev else "  拒收")
        db.close()
        return 0
    ev = R.record(db, action=args.action, now=now,
                  partner=args.partner, src="manual")
    if ev is None:
        print(f"  拒收：动作不在 {sorted(R.ACTIONS)} 里，或名字不像名字")
        db.close()
        return 2
    print(f"  记下了：{args.action}")
    db.close()
    return 0


def cmd_character(args: argparse.Namespace) -> int:
    """角色事实：年龄推导，其余可读写。**是记录，不是记忆。**"""
    import time

    from gits.core.db import DB
    from gits.memory import character as C

    db = DB(Path(args.db))
    now = int(time.time() * 1000)
    if args.key:
        if args.value is None:
            print("  要给一个值")
            return 2
        ok, why = C.bounded(db, key=args.key, value=args.value, now=now)
        if not ok:
            print(f"  拒收：{why}")
            return 2
        # **跨字段自洽。** 手工写入是唯一能一次塞进一整组数字的入口，
        # 也正是不自洽最容易进来的地方（导入、粘贴）。
        trial = dict(C.current(db))
        trial[args.key] = str(args.value)
        bad = C.problems(trial, now=now)
        if bad:
            print("  拒收：这套数字自相矛盾")
            for b in bad:
                print(f"    · {b}")
            return 2
        ev = C.record(db, key=args.key, value=args.value, src="user", now=now)
        print(f"  {'记下了' if ev else '没记（键不在白名单里）'}：{args.key}={args.value}")
        return 0 if ev else 2
    cur = C.current(db)
    if not cur:
        print("  （还没有角色事实）")
        return 0
    for k, v in sorted(cur.items()):
        print(f"  {k} = {v}")
    a = C.age(db, now=now)
    if a is not None:
        print(f"  （年龄按生日推导：{a}）")
    return 0


def cmd_world(args: argparse.Namespace) -> int:
    """外面的世界：她此刻读到的天气。**读数落成事件，不是实时查询。**"""
    import time

    from gits.core.db import DB
    from gits.world import amap

    db = DB(Path(args.db))
    try:
        key, adcode = amap.config()
    except RuntimeError as exc:
        print(f"  {exc}")
        return 2
    now = int(time.time() * 1000)
    if args.fetch:
        got = amap.maybe(db, now=now, key=key, adcode=adcode)
        print(f"  采集：{'取到 ' + str(got.get('weather')) if got else '没有新观测（未过期或取不到）'}")
    r = amap.latest(db, now=now)
    if r is None:
        print("  还没有任何读数")
        return 1
    fresh = not amap._stale(r, now=now, ttl=amap.FRESH_MS)
    print(f"  观测：{r.get('city')} {r.get('weather')} {r.get('temp')} 度 "
          f"湿度 {r.get('humidity')} {r.get('wind')}风 {r.get('power')} 级")
    print(f"  时间：{r.get('report')}  {'新鲜' if fresh else '已过期（不进上下文）'}")
    print(f"  B 段一行：{amap.line(db, now=now) or '（空——不知道就不说）'}")
    if args.verify_location:
        ip = amap.fetch_ip(key=key)
        same = ip.get("adcode") == adcode
        print(f"  IP 定位：{ip.get('province')}{ip.get('city')}（{ip.get('adcode')}）"
              f"  {'与配置一致' if same else '与配置不符——IP 是机房位置，以配置为准'}")
    return 0


def cmd_schema_check(args: argparse.Namespace) -> int:
    """**生产库的结构和仓库里的迁移对得上吗。**

    被两件事逼出来的：018 改过之后生产库缺 affect_state；018 又改过之后
    affect 多了一列 last_ts。两次都是"文件改了、库没变"，而单元测试永远看不到
    ——测试库是从零迁移的。

    做法很笨但直接：拿一个**空的临时库**跑完全部迁移，然后逐表逐列比对。
    """
    import sqlite3
    import tempfile
    from pathlib import Path as _P

    def shape(conn) -> dict[str, list[tuple]]:
        out = {}
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"):
            out[name] = [(r[1], (r[2] or "").upper(), r[3], r[4])
                         for r in conn.execute(f"PRAGMA table_info({name})")]
        return out

    with tempfile.TemporaryDirectory() as tmp:
        fresh_path = _P(tmp) / "fresh.db"
        DB(fresh_path).close()
        fresh = shape(sqlite3.connect(fresh_path))
    live = shape(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True))

    missing = sorted(set(fresh) - set(live))
    extra = sorted(set(live) - set(fresh))
    drift = []
    for name in sorted(set(fresh) & set(live)):
        f_cols = {c[0]: c for c in fresh[name]}
        l_cols = {c[0]: c for c in live[name]}
        for col, spec in f_cols.items():
            if col not in l_cols:
                drift.append(f"{name}.{col} 缺（{spec[1]}，notnull={spec[2]}）")
            elif l_cols[col][1] != spec[1]:
                drift.append(f"{name}.{col} 类型 {l_cols[col][1]} != {spec[1]}")
        for col in sorted(set(l_cols) - set(f_cols)):
            drift.append(f"{name}.{col} 多出来（库里 {l_cols[col][1]}"
                         f"，notnull={l_cols[col][2]}）")

    # **"多出来的表"只提示，不算错。**FTS 的影子表（node_fts_*）、旧版本留下的表
    # 都在这一类里；它们不影响现在的代码，报成错误只会让人习惯性忽略这个检查。
    if extra:
        print("（多出来的表，不影响使用：" + ", ".join(extra) + "）")
    if not (missing or drift):
        print(f"结构一致：{len(fresh)} 张表")
        return 0
    if missing:
        print("缺表：" + ", ".join(missing))
    for d in drift:
        print("  " + d)
    print(f"\n{len(drift) + len(missing)} 处不一致。"
          "改老迁移对已有库无效——新结构要加新文件。")
    return 1


def cmd_backup(args: argparse.Namespace) -> int:
    """做一份备份并验证它。**坏的备份比没有备份更危险。**"""
    from gits.core import backup

    args.dest = args.dest or backup.DEFAULT_DEST
    args.keep = args.keep or backup.DEFAULT_KEEP
    if args.verify:
        ok, why = backup.verify(args.verify)
        print(("  可用：" if ok else "  不能用：") + why)
        return 0 if ok else 1

    got = backup.run(args.db, dest=args.dest, keep=args.keep)
    if not got["ok"]:
        print(f"  备份失败：{got.get('error') or got.get('integrity')}")
        return 1
    print(f"  备份好了  {got['path']}")
    print(f"  {got['bytes'] / 1024:.0f} KB · events={got['events']}"
          f" · integrity={got['integrity']} · sha256 {got['sha256']}")
    print(f"  最近一份：{backup.latest(args.dest)}")
    return 0


def cmd_schema_manifest(args: argparse.Namespace) -> int:
    """重新登记迁移文件的指纹。**改了老迁移之后才需要跑这个。**

    迁移是只读的：已经应用过的那份不会重跑，改它就只是让新库和老库不一样。
    新结构一律加新文件；真要动老文件，跑一次这个，并把原因写进提交说明。
    """
    import hashlib
    from pathlib import Path as _P

    from gits.core.db import SCHEMA_DIR

    lines = []
    for path in sorted(_P(SCHEMA_DIR).glob("[0-9]*_*.sql")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    out = _P(SCHEMA_DIR) / "MANIFEST.sha256"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"登记了 {len(lines)} 个迁移 -> {out}")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    """缓存诊断：命中率 + **是哪一段在动**。

    命中率低的时候，第一个问题不是"怎么调参数"，是"哪一段变了"。
    没有这个数就只能猜——而猜出来的调参通常会把正常的东西也改掉。
    """
    from gits.speech import shape

    db = DB(args.db)
    now = int(time.time() * 1000)
    s = shape.stats(db, now=now)
    hr = "?" if s["hit_rate"] is None else f"{s['hit_rate']:.0%}"
    print(f"最近 24 小时  {s['turns']} 轮  命中率 {hr}  窗口 {s['windows']} 个")
    print(f"  A 段（人格）变了 {s['a_versions']} 次")
    print(f"  B 段（关系/情绪）变了 {s['b_versions']} 次"
          f"（同一窗口内最多 {s['b_per_window']} 次）")
    print(f"  C 段（这一轮）变了 {s['c_versions']} 次 —— 每轮都变是正常的")
    lines = shape.diagnose(db, now=now)
    if lines:
        print()
        for line in lines:
            print(line)
    else:
        print("\n前缀是稳的。")
    db.close()
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    """额度账户。像电子账户一样，规则全部可见。"""
    from gits.io import ledger
    from gits.io.quota import bar

    db = DB(args.db)
    now = int(time.time() * 1000)
    print(ledger.statement(db, actor=args.actor, now=now))
    st = ledger.state(db, actor=args.actor, now=now)
    cap = st["day_cap"]
    pct = 0.0 if cap <= 0 else min(1.0, st["spent_today"] / cap)
    print(f"\n  给她看的一行：额度{bar(pct, 8)} {st['spendable']:.1f}/{cap:.1f}元")
    db.close()
    return 0


def _flag(name: str) -> bool:
    """环境变量开关。1/true/yes 都算开——部署时手打，宽容一点。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def cmd_ghost(args: argparse.Namespace) -> int:
    """常驻内核：心跳 + 快照。投递在 shell 里，不在这里。"""
    import asyncio as _asyncio

    from .ghost import serve

    if args.outbox_file:
        print("警告：--outbox-file 已废弃，投递搬到 gits shell", file=sys.stderr)
    with _open(args):
        pass                                  # 只为建库/迁移，随后 serve 自己开
    return _asyncio.run(serve(args.db, node=args.node, once=args.once,
                              persona_apply=(args.persona_apply
                                             or _flag("GITS_PERSONA_APPLY"))))


def cmd_shell(args: argparse.Namespace) -> int:
    """通道。往外投递 outbox，往里收消息。"""
    import asyncio as _asyncio

    from gits.io.shell import FileChannel, WeixinAdapter, serve

    channels = []
    if args.weixin_state:
        from gits.io.channels.weixin import Account

        acct = Account.load(args.weixin_state)
        channels.append(WeixinAdapter(acct,
                                      cursor_path=Path(args.weixin_state).with_suffix(".cursor.json")))
        print(f"微信通道已挂载：{acct.user_id}", file=sys.stderr)
    if args.out:
        channels.append(FileChannel(args.out, args.inbox))
    if not channels:
        print("没有可用通道", file=sys.stderr)
        return 1

    with _open(args):
        pass
    return _asyncio.run(serve(args.db, channels=channels, node=args.node,
                              once=args.once))

def cmd_say_out(args: argparse.Namespace) -> int:
    """往 outbox 里塞一条，用来手工验证投递链路。

    带 `--weixin` 时从 contacts 表取 `context_token`——**回复必须原样带回**，
    否则消息挂不到会话上。
    """
    with _open(args) as db:
        meta = {}
        if args.weixin:
            row = db.one("SELECT actor_id, last_context_token FROM contacts"
                         " WHERE channel='weixin' ORDER BY last_seen DESC LIMIT 1")
            if row is None:
                print("还没有收到过微信消息，拿不到 context_token", file=sys.stderr)
                return 1
            meta = {"to": row["actor_id"], "context_token": row["last_context_token"]}
        w = Writer(db)
        with w.txn() as c:
            cur = c.execute(
                "INSERT INTO outbox(ts, channel, text, status, meta)"
                " VALUES(?,?,?, 'pending', ?)",
                (now_ms(), args.channel, args.text,
                 json.dumps(meta, ensure_ascii=False) if meta else None))
            print(json.dumps({"outbox_id": cur.lastrowid, "channel": args.channel,
                              "to": meta.get("to")}, ensure_ascii=False))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """模拟"他回了一句"：往投递箱追加一行。"""
    item = {"text": args.text, "ext_id": args.ext_id or f"m{now_ms()}"}
    with open(args.file, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps(item, ensure_ascii=False))
    return 0


def cmd_weixin(args: argparse.Namespace) -> int:
    """微信通道：登录、查看、发一条。"""
    from gits.io.channels.weixin import Account, WeixinClient, WeixinChannel

    state = Path(args.state)
    client = WeixinClient()

    if args.action == "login":
        d = client.get_qrcode()
        if d.get("ret") not in (0, None):
            print("取二维码失败:", d, file=sys.stderr)
            return 1
        qr, url = d["qrcode"], d.get("qrcode_img_content", "")
        print(json.dumps({"qrcode": qr, "url": url}, ensure_ascii=False), flush=True)
        print("请用手机微信扫码：", url, flush=True)

        deadline = time.time() + args.wait
        while time.time() < deadline:
            st = client.poll_qr(qr)
            s = st.get("status")
            if s in ("scaned", "wait"):
                continue
            if s == "confirmed":
                acct = Account(token=st.get("bot_token", ""),
                               base_url=st.get("baseurl") or "https://ilinkai.weixin.qq.com",
                               ilink_bot_id=st.get("ilink_bot_id", ""),
                               user_id=st.get("ilink_user_id", ""))
                acct.save(state)
                print("登录成功:", json.dumps(
                    {"ilink_bot_id": acct.ilink_bot_id, "user_id": acct.user_id,
                     "base_url": acct.base_url}, ensure_ascii=False))
                return 0
            if s in ("expired", "need_verifycode", "verify_code_blocked"):
                print("扫码失败/过期:", s)
                return 2
        print("等待超时")
        return 3

    if not state.exists():
        print("还没登录：先跑 gits weixin login", file=sys.stderr)
        return 1
    account = Account.load(state)

    if args.action == "whoami":
        c = WeixinClient(account)
        print(json.dumps({"account": account.user_id, "bot": account.ilink_bot_id,
                          "base": account.base_url}, ensure_ascii=False))
        try:
            cfg = c.get_config()
            print("typing_ticket:", "有" if cfg.get("typing_ticket") else "无")
        except Exception as exc:
            print("getconfig 失败:", type(exc).__name__, file=sys.stderr)
        return 0

    if args.action == "send":
        if not args.to:
            print("需要 --to", file=sys.stderr)
            return 1
        resp = WeixinClient(account).send_text(args.to, args.text,
                                               args.context_token)
        print(json.dumps(resp, ensure_ascii=False))
        return 0
    return 1

def cmd_recall(args: argparse.Namespace) -> int:
    """她此刻能想起什么。走真实的 nodes / node_content / FTS / r。"""
    from gits.memory.recall import recall
    with _open(args) as db:
        rows = recall(db, args.query, actor="her", context=args.context or "",
                      now_ms=args.now or now_ms(), k=args.k)
        if not rows:
            print("（什么都没想起来）")
            return 0
        for r in rows:
            print(f"{r.score:5.3f}  r={r.r:4.2f}  {r.line()}")
    return 0


def cmd_turn(args: argparse.Namespace) -> int:
    """拼出一轮的 prompt（A/B/C）。用来看「她记得什么」到底长什么样。"""
    from gits.speech.context import PersonaSeed, RelationSnapshot, TurnContext, assemble, bucket
    from gits.memory.recall import lines, recall
    with _open(args) as db:
        t = args.now or now_ms()
        rows = recall(db, args.text, actor="her", context=args.context or "", now_ms=t, k=args.k)
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
    from gits.core.replica import pull
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
    from gits.core.replica import verify
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

    pr = sub.add_parser("peers", help="谁存在、它们之间发生过什么")
    pr.add_argument("--add", default=None, help="登记一个 peer（不给就只看）")
    pr.add_argument("--seed", default=None,
                    help="JSON：{\"话题\": 权重}，只对 --add 生效")
    pr.set_defaults(fn=cmd_peers)

    sd = sub.add_parser("seed", help="导入初始设定（写成一事件，不是文件）")
    sd.add_argument("text", nargs="?", default="")
    sd.add_argument("--actor", default="her")
    sd.add_argument("--topic", action="append", default=[],
                    metavar="话题=权重", help="初始注意力，可重复")
    sd.set_defaults(fn=cmd_seed)

    pn = sub.add_parser("persona", help="她现在的自我描述 / 从哪几条来")
    pn.add_argument("--snapshot", action="store_true", help="现在就重选一次")
    pn.set_defaults(fn=cmd_persona)

    im = sub.add_parser("import-seed", help="导入初始化包（默认只校验）")
    im.add_argument("path", help="文件路径，或 - 从 stdin 读（推荐：不落盘）")
    im.add_argument("--write", action="store_true", help="真的写进去")
    im.add_argument("--inspect", action="store_true",
                    help="校验时打印话题名（默认不打——话题名也是内容）")
    im.add_argument("--allow-ephemeral", action="store_true",
                    help="允许导入带 ephemeral 标记的示例包")
    im.set_defaults(fn=cmd_import_seed)

    wp = sub.add_parser("wipe", help="清空内容，保留机制（要 --yes）")
    wp.add_argument("--yes", action="store_true", help="真的清（否则只报告）")
    wp.add_argument("--actor", default="her")
    wp.set_defaults(fn=cmd_wipe)

    al = sub.add_parser("aliases", help="语义别名：谁被当成同一个东西了")
    al.add_argument("--inspect", action="store_true",
                    help="打印实体名（默认只给 id——名字也是内容）")
    al.add_argument("--merge", nargs=2, metavar=("别名", "归到"),
                    help="手工合并两个实体（会写一条可重放的事件）")
    al.set_defaults(fn=cmd_aliases)

    fn = sub.add_parser("funnel", help="漏斗：该压谁、压到多深")
    fn.add_argument("--run", type=int, default=0, help="真的压几个")
    fn.set_defaults(fn=cmd_funnel)

    t = sub.add_parser("peer-turn", help="给某个实例跑一个回合（回应或不回应）")
    t.add_argument("actor")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=cmd_peer_turn)

    s = sub.add_parser("say")
    s.add_argument("text")
    s.add_argument("--source", default="wechat")
    s.add_argument("--ext-id", default=None)
    s.set_defaults(fn=cmd_say)

    r = sub.add_parser("replay", help="恢复重放：清空投影，只用日志集重建")
    r.add_argument("--restore", action="store_true",
                   help="（唯一的模式，保留以便以后加 --experiment）")
    r.add_argument("--shadow", action="store_true",
                   help="在副本上重放做校验，不动线上库（夜间任务该用这个）")
    r.set_defaults(fn=cmd_replay)

    t = sub.add_parser("trace")
    t.add_argument("outbox_id", type=int)
    t.set_defaults(fn=cmd_trace)

    ig = sub.add_parser("ingress", help="位置入口：唯一对公网开的门")
    ig.add_argument("--port", type=int, default=8443)
    ig.set_defaults(fn=cmd_ingress)
    lc = sub.add_parser("loc", help="手机上报一次位置（我们接收，不追踪）")
    lc.add_argument("--lat", type=float)
    lc.add_argument("--lon", type=float)
    lc.add_argument("--acc", type=float)
    lc.add_argument("--source", default=None)
    lc.add_argument("--stdin", action="store_true", help="从 stdin 读 termux-location 的 JSON")
    lc.set_defaults(fn=cmd_loc)
    ch = sub.add_parser("character", help="角色事实：年龄/身高/体重（记录，不是记忆）")
    ch.add_argument("key", nargs="?")
    ch.add_argument("value", nargs="?")
    ch.set_defaults(fn=cmd_character)
    rl = sub.add_parser("relation", help="关系账本：只能加，不能改，不能减")
    rl.add_argument("action", nargs="?", default=None,
                    help="meet|kiss|confess|intimacy|breakup|seed，不给就看账本")
    rl.add_argument("partner", nargs="?", default="他")
    rl.add_argument("--count", type=int, default=0, help="seed 用：前史次数")
    rl.set_defaults(fn=cmd_relation)
    wd = sub.add_parser("world", help="外面的世界：天气/位置（读数是事件）")
    wd.add_argument("--fetch", action="store_true", help="现在去取一次")
    wd.add_argument("--verify-location", action="store_true",
                    help="用 IP 定位核对配置（只核对，不覆盖）")
    wd.set_defaults(fn=cmd_world)
    q = sub.add_parser("quota", help="额度账户（像电子账户一样可见）")
    q.add_argument("--actor", default="her")
    q.add_argument("--monthly", type=float, default=200.0)
    q.add_argument("--balance", type=float, default=0.0)
    q.set_defaults(fn=cmd_quota)
    sc = sub.add_parser("schema-check", help="生产库结构和迁移对得上吗")
    sc.set_defaults(fn=cmd_schema_check)
    bk = sub.add_parser("backup", help="做一份备份并验证它")
    bk.add_argument("--dest", default=None)
    bk.add_argument("--keep", type=int, default=None)
    bk.add_argument("--verify", default=None, help="验一份已有的备份")
    bk.set_defaults(fn=cmd_backup)
    sm = sub.add_parser("schema-manifest",
                        help="重新登记迁移指纹（只该在改老迁移时用）")
    sm.set_defaults(fn=cmd_schema_manifest)
    ca = sub.add_parser("cache", help="缓存诊断：命中率 + 哪一段在动")
    ca.set_defaults(fn=cmd_cache)

    sy = sub.add_parser("sync", help="从另一份库拉增量（单向、可断点续传）")
    sy.add_argument("--src", required=True)
    sy.add_argument("--node", default="replica")
    sy.add_argument("--batch", type=int, default=500)
    sy.add_argument("--max-rounds", type=int, default=50)
    sy.set_defaults(fn=cmd_sync)

    vf = sub.add_parser("verify", help="比较两份库重放后的投影")
    vf.add_argument("--src", required=True)
    vf.set_defaults(fn=cmd_verify)

    gh = sub.add_parser("ghost", help="常驻内核（心跳/投递/快照）")
    gh.add_argument("--node", default="cloud")
    gh.add_argument("--persona-apply", action="store_true",
                    help="真的写人格新版本。默认只判定不动作（观察期）")
    gh.add_argument("--outbox-file", default=None, help="投递到文件（没有真通道时）")
    gh.add_argument("--once", action="store_true", help="只跑一拍就退出（测试用）")
    gh.set_defaults(fn=cmd_ghost)

    sh = sub.add_parser("shell", help="通道（投递 + 收消息）")
    sh.add_argument("--out", default="var/outbox.jsonl")
    sh.add_argument("--inbox", default="var/inbox.jsonl")
    sh.add_argument("--weixin-state", default=None, help="挂了微信通道的凭据文件")
    sh.add_argument("--node", default="cloud")
    sh.add_argument("--once", action="store_true")
    sh.set_defaults(fn=cmd_shell)

    so = sub.add_parser("say-out", help="手工往 outbox 塞一条")
    so.add_argument("text")
    so.add_argument("--channel", default="desk_audio")
    so.add_argument("--weixin", action="store_true",
                    help="从 contacts 取 context_token，走微信发")
    so.set_defaults(fn=cmd_say_out)

    ib = sub.add_parser("inbox", help="模拟他回了一句")
    ib.add_argument("text")
    ib.add_argument("--file", default="var/inbox.jsonl")
    ib.add_argument("--ext-id", default=None)
    ib.set_defaults(fn=cmd_inbox)

    wx = sub.add_parser("weixin", help="微信通道：登录 / 查看 / 发送")
    wx.add_argument("action", choices=["login", "whoami", "send"])
    wx.add_argument("--state", default="var/weixin.json")
    wx.add_argument("--wait", type=float, default=180.0)
    wx.add_argument("--to", default=None)
    wx.add_argument("--text", default="")
    wx.add_argument("--context-token", default=None)
    wx.set_defaults(fn=cmd_weixin)

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


def cmd_import_seed(args: argparse.Namespace) -> int:
    from gits.core import seedfile

    db = DB(Path(args.db))
    path: Path | None = None
    if args.path == "-":
        # 从管道读：明文**根本不落盘**，也就不需要 care 它放在哪
        text = sys.stdin.read()
    else:
        path = Path(args.path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"读不到：{exc}")
            return 1
    try:
        bundle = seedfile.parse(text)
    except seedfile.Bad as exc:
        print(f"拒绝：{exc}")
        print("（整份拒绝，一条都没写）")
        return 1
    print(seedfile.describe(bundle, inspect=args.inspect))
    # **先查 ephemeral，再查路径。**反过来的话，一个被误写进仓库的示例包
    # 会报「文件在仓库里」——而真正的问题（它是示例包）就被那句话盖住了。
    if bundle.ephemeral and not args.allow_ephemeral:
        print("\n这是示例包（ephemeral），生产环境默认不收。"
              "真要导就加 --allow-ephemeral。")
        return 1
    if path is not None and seedfile.inside_repo(path):
        print("\n这个文件在仓库里。换到仓库外面再导——文件不进来，状态才进来。")
        return 1
    if not args.write:
        print("\n只是校验。要真写就加 --write。")
        return 0

    counts = seedfile.apply(db, bundle)
    print("\n写进去了：" + json.dumps(counts, ensure_ascii=False))
    gap = seedfile.projection_gap(db)
    if gap:
        print("\n⚠ 投影还没跟上：%d 条自我描述还只在事件里。" % gap)
        print("   A 段现在仍然是空的。跑这一行：")
        print(f"   gits --db {args.db} replay --restore")
    else:
        print("投影已经跟上，A 段生效。")
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    from gits.memory import persona
    from gits.core import wipe

    db = DB(Path(args.db))
    before = wipe.states(db, actor=args.actor)
    print("现在的内容：" + (json.dumps(before, ensure_ascii=False) if before
                            else "（已经没有内容了）"))
    if not before:
        return 0
    if not args.yes:
        print("\n只是报告。真要清就加 --yes。")
        print("注意：清完之后 A 段是空的，她不会回话——这是刻意的。")
        return 0
    got = wipe.wipe(db, actor=args.actor)
    print("\n清掉了：" + json.dumps(got, ensure_ascii=False))
    print("机制都还在：表、代码、调度、漏斗、漂移。只缺内容。")
    cur = persona.current(db, actor=args.actor)
    print("A 段：" + ("空——她不会说话" if cur is None
                      else repr(cur["narrative"][:40])))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from gits.social import actors
    from gits.social import attention
    from gits.memory.persona import seed

    db = DB(Path(args.db))
    if not args.text and not args.topic:
        print("给点什么：一段文字，和/或 --topic 话题=权重")
        return 1
    # 一个真会咬人的歧义：--topic A=1 B=2 时，B=2 会被当成**人格正文**吞下去。
    # 「不报错但结果错」的典型——所以这里直接拒绝。
    if (args.topic and args.text and "=" in args.text
            and args.text.split("=", 1)[1].replace(".", "", 1).isdigit()):
        print(f"「{args.text}」看起来是偏好不是正文。要多写几条请重复 --topic。")
        return 1
    actors.ensure(db, args.actor, is_main=(args.actor == actors.MAIN))
    if args.text:
        ev = seed(db, args.text, actor=args.actor)
        print(f"种下第 {ev} 条事件（scope=self:{args.actor}）")
        print("  —— 它是一条普通记忆：会衰减、会被顶掉、也会被压进摘要。")
    if args.topic:
        weights = {}
        for item in args.topic:
            if "=" not in item:
                print(f"看不懂「{item}」，要写成 话题=权重")
                return 1
            name, val = item.split("=", 1)
            weights[name.strip()] = float(val)
        n = attention.seed(db, args.actor, weights)
        print(f"种下 {n} 个注意力偏好（会漂，不固定）")
    return 0


def cmd_persona(args: argparse.Namespace) -> int:
    from gits.memory import persona

    db = DB(Path(args.db))
    if args.snapshot:
        got = persona.snapshot(db)
        if got is None:
            print("没有可选的自我域节点——先 gits seed")
            return 1
        print(f"新的一版：{len(got.picked)} 个节点 → 事件 {got.event_id}")
    cur = persona.current(db)
    if cur is None:
        print("还没有自我描述（A 段是空的，她不会说话）")
        return 0
    print(f"v{cur['version']}（事件 {cur['event_id']}）")
    print(cur["narrative"])
    cands = persona.candidates(db, now=now_ms())
    print(f"\n现在能想起来 {len(cands)} 条自我域记忆：")
    for c in cands[:persona.SLOTS]:
        print(f"  r={c['r']:.2f} depth={c['depth']}  {c['content'][:40]}")
    return 0


def cmd_aliases(args: argparse.Namespace) -> int:
    from gits.memory import semantic
    from gits.core.project import entity_id

    db = DB(Path(args.db))
    if args.merge:
        alias, canonical = args.merge
        ev = semantic.merge(db, entity_id(alias), entity_id(canonical),
                            source="manual", now=now_ms())
        print(f"合并了（事件 {ev}）。重放一遍让簇跟着变：gits --db {args.db} replay --restore")
        return 0

    names = {}
    if args.inspect:
        for r in db.q("SELECT c.node_id, c.content FROM node_content c"
                      " JOIN nodes n ON n.id = c.node_id WHERE n.kind='entity'"):
            names[str(r["node_id"])] = str(r["content"])

    def show(eid: str) -> str:
        return names.get(eid, eid) if args.inspect else eid

    rows = db.q("SELECT * FROM entity_alias WHERE superseded_by IS NULL")
    print(f"已合并 {len(rows)} 条：")
    for r in rows:
        print(f"  {show(str(r['alias_id']))} → {show(str(r['canonical']))}"
              f"  （{r['source']}, 确信 {r['certainty']:.1f}）")

    cands = semantic.propose(db)
    print(f"\n共现推断的候选 {len(cands)} 条（**只是候选，没有合并**）：")
    for a, b, score in cands:
        print(f"  {show(a)} ~ {show(b)}  相似度 {score}")
    if cands:
        print("\n要合的话：gits aliases --merge <别名> <归到>，或者等夜里压缩时让模型看一眼")
    return 0


def cmd_funnel(args: argparse.Namespace) -> int:
    from gits.memory import funnel

    db = DB(Path(args.db))
    t = now_ms()
    hist = funnel.depth_histogram(db)
    print("深度分布：" + ("  ".join(f"{d}:{n}" for d, n in sorted(hist.items()))
                          or "（空）"))
    if len(hist) > 1:
        levels = [hist[d] for d in sorted(hist)]
        mono = all(a >= b for a, b in zip(levels, levels[1:]))
        print("  " + ("递减 ✓ 是漏斗" if mono else "不递减 ✗ 只是摘要堆"))
    jobs = funnel.plan(db, now=t)
    print(f"\n该压的：{len(jobs)}")
    for name, why in jobs:
        print(f"  {name}  （{why}）")
    if args.run:
        got = funnel.run(db, now=t, limit=args.run)
        for g in got:
            print(f"  压了 {g.entity}：{g.children} 条 → {g.summary[:30]}"
                  f"（压缩比 {g.ratio}）")
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    from gits.social import actors
    from gits.social import attention
    from gits.social import peer

    db = DB(Path(args.db))
    actors.ensure(db, actors.MAIN, is_main=True)
    if args.add:
        actors.ensure(db, args.add)
        if args.seed:
            n = attention.seed(db, args.add, json.loads(args.seed))
            print(f"登记 {args.add}，初始偏好 {n} 个话题")
        else:
            print(f"登记 {args.add}")
    print("实例：")
    for a in actors.all_of(db):
        mark = "（主，有对外通道）" if a.is_main else ""
        w = attention.weights(db, a.actor_id)
        pref = "、".join(f"{k}={v:.2f}" for k, v in w.items()) or "（没有初始偏好）"
        print(f"  {a.actor_id}{mark}  {pref}")
    obs = peer.observe(db)
    print("发生过的事：")
    print(f"  第一次主动    {obs['first_contact_ts'] or '还没有'}")
    print(f"  每对条数      {obs['by_pair'] or '无'}")
    print(f"  沉默的回合    {obs['silent']}")
    print(f"  选项露出次数  {obs['affordances']}")
    print(f"  没说出口      {obs['unsaid']}")
    if obs["affordances"] and not obs["first_contact_ts"]:
        print("  → 露过面但还没人开口。**这就是数据**，不是故障。")
    return 0


def cmd_peer_turn(args: argparse.Namespace) -> int:
    from gits.social import actors
    from gits.social import peer

    db = DB(Path(args.db))
    turn = peer.received(db, args.actor)
    if not turn:
        print(f"{args.actor} 现在没有轮到它说话")
        return 0
    ev = turn[0]
    text = (ev["payload"] or b"").decode("utf-8")
    print(f"[{ev['source']}] {text}")
    if args.dry_run:
        return 0
    peer.silent(db, args.actor, ev)          # P0：先只记回合，不生成内容
    print("（这一版还不生成内容：只记了一个回合）")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
