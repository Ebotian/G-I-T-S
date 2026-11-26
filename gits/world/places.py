"""场所：**从轨迹里学出来的，不是他填的。**

三件事，每件对应一个会坏的地方：

  1 聚类      网格 + 半径合并（numpy 够，不用 scipy）
              半径跟精度走：30 m 的定位用 60 m 半径，家和公司才分得开。

  2 去抖      **观测不等于确认。** 同一个房间 WiFi 切流量，出口从家宽变运营商，
              轨迹上会"跳 40 公里再跳回来"。所以确认要连续 N 次同向。
              这和 slew 是同一种精神（不信任单次观测），但作用在**离散量**上，
              所以手法是迟滞，不是限幅。

  3 命名      **不自动命名。** 从"夜里 90% 在这"推断出"这是家"是猜；
              猜错了她会在你上班时问"到家了？"。名字只能来自他说过的话。

成员资格永久：一个坐标属于过某个地方，就一直是（和边的成员资格同一条纪律）。
"""

from __future__ import annotations

import math
from typing import Any

from gits.core.db import DB, Writer

#: 只有这些来源能进场所层。ip 是城市级，会把家和公司合并成一个点。
PLACE_SOURCES = frozenset({"gps", "wifi", "network"})
#: 精度差于这个的定位不参与场所（但仍然是读数）
MAX_ACC = 300.0
#: 默认半径。30 m 的定位用 60 m，够分开相邻的两个常去地点。
RADIUS = 60.0
#: 连续几次同向才改判"他在哪"
CONFIRM_N = 2
#: 一个地方至少要有这么多条定位才被当成"地方"。
#: **证据广度**——网络切换造成的一次 40 公里跳点不该长成一个场所。
MIN_FIXES = 3
#: **停留时长**：至少连续待够这么久才算一个地方。
#:
#: 注意这里是**时长不是次数**。上报是"位置变了才触发"，不是定时轮询：
#: 他在家待一整天可能只有两条定位，通勤来回走却有一串。按次数算，
#: 跳点攒一周有 16 条，家可能只有 2 条——**最要紧的地方看起来最不像地方**。
MIN_DWELL_MS = 30 * 60_000
#: 一条定位最多代表多长时间（超过就认为中间断了，不连续）
MAX_GAP_MS = 6 * 3_600_000
#: 步长。度 -> 米（纬度）；经度要乘 cos(lat)
M_PER_DEG = 111_320.0


def _dist_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """平面近似。单城市场景够用——我们不需要跨半球。"""
    dlat = (a_lat - b_lat) * M_PER_DEG
    dlon = (a_lon - b_lon) * M_PER_DEG * math.cos(math.radians(a_lat))
    return math.hypot(dlat, dlon)


def _readings(db: DB, *, after: int, limit: int = 5000) -> list[dict]:
    import json

    out = []
    for row in db.q(
            "SELECT id, payload FROM events WHERE kind='reading' AND id>?"
            " ORDER BY id LIMIT ?", (after, limit)):
        try:
            r = json.loads((row["payload"] or b"{}").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if r.get("kind") != "loc":
            continue
        r["_event_id"] = int(row["id"])
        out.append(r)
    return out


def _cursor(db: DB, name: str) -> int:
    v = db.scalar("SELECT last_event_id FROM learn_cursor WHERE name=?", (name,))
    return int(v or 0)


def _set_cursor(c: Any, name: str, ev: int) -> None:
    c.execute("INSERT INTO learn_cursor(name, last_event_id) VALUES(?,?)"
              " ON CONFLICT(name) DO UPDATE SET last_event_id=?", (name, ev, ev))


def learn(db: DB, *, step: int = 200) -> int:
    """增量学。**幂等**：游标之后才算，重复调用不重复计数。

    重建 = 清空 places / place_fixes / place_state / 游标，再从头 learn()。
    """
    n = 0
    while True:
        after = _cursor(db, "places")
        rows = _readings(db, after=after, limit=step)
        if not rows:
            return n
        with Writer(db).txn() as c:
            for r in rows:
                _ingest(c, r)
                n += 1
            _set_cursor(c, "places", max(r["_event_id"] for r in rows))


def _ingest(c: Any, r: dict) -> None:
    src = r.get("source")
    if src not in PLACE_SOURCES:
        return
    acc = r.get("acc")
    acc = float(acc) if acc is not None else RADIUS
    if acc > MAX_ACC:
        return
    lat, lon = float(r["lat"]), float(r["lon"])
    ts = int(r.get("fix_ts") or 0)

    prev = c.execute("SELECT place_id FROM place_fixes"
                     " ORDER BY event_id DESC LIMIT 1").fetchone()
    prev_pid = int(prev["place_id"]) if prev else -1

    best, best_d = None, None
    for row in c.execute("SELECT id, lat, lon, radius, n, w, first_ts, last_ts"
                         " FROM places WHERE scope='self:her'").fetchall():
        d = _dist_m(lat, lon, row["lat"], row["lon"])
        reach = max(float(row["radius"]), 1.5 * acc)
        if d <= reach and (best_d is None or d < best_d):
            best, best_d = row, d

    if best is None:
        cur = c.execute(
            "INSERT INTO places(scope, lat, lon, radius, n, w, first_ts, last_ts,"
            " run, best_run, run_dwell_ms, best_dwell_ms)"
            " VALUES('self:her',?,?,?,1,?,?,?,1,1,0,0)",
            (lat, lon, max(RADIUS, 1.5 * acc), 1.0 / (acc * acc + 1),
             ts or 0, ts or 0))
        pid = int(cur.lastrowid)
    else:
        pid = int(best["id"])
        w = 1.0 / (acc * acc + 1)
        tw = float(best["w"]) + w
        run = 1 if prev_pid != pid else int(
            c.execute("SELECT run FROM places WHERE id=?", (pid,)).fetchone()["run"]) + 1
        c.execute(
            "UPDATE places SET lat=(lat*?+?*?)/?, lon=(lon*?+?*?)/?,"
            " n=n+1, w=?, last_ts=max(last_ts, ?), radius=max(radius, ?),"
            " run=?, best_run=max(best_run, ?)"
            " WHERE id=?",
            (float(best["w"]), lat, w, tw,
             float(best["w"]), lon, w, tw,
             tw, ts or 0, max(RADIUS, 1.5 * acc), run, run, pid))
    # **先把上一条定位代表的那段时长结算掉。**
    # 上报是位置变了才触发，所以"一条定位"= "从它到下一条"的那段时间。
    # **只有下一条定位还在同一个地方，这段时长才归这个地方。**
    # 否则不知道他是什么时候走的——把整段算给前一个地点，等于替一次跳点
    # 认领了它之后那半小时（实测：30 分钟采样间隔下，单点跳点会被认成"待了 30 分钟"）。
    last = c.execute("SELECT place_id, event_id, ts FROM place_fixes"
                     " ORDER BY event_id DESC LIMIT 1").fetchone()
    if last is not None and ts and int(last["place_id"]) == pid:
        gap = int(ts) - int(last["ts"] or 0)
        if 0 < gap <= MAX_GAP_MS:
            c.execute("UPDATE place_fixes SET dwell_ms=? WHERE event_id=?",
                      (gap, int(last["event_id"])))
            row = c.execute("SELECT run_dwell_ms FROM places WHERE id=?",
                            (pid,)).fetchone()
            run_dwell = int(row["run_dwell_ms"]) + gap
            c.execute("UPDATE places SET run_dwell_ms=?,"
                      " best_dwell_ms=max(best_dwell_ms, ?) WHERE id=?",
                      (run_dwell, run_dwell, pid))

    # 成员资格永久：只加不删
    c.execute("INSERT INTO place_fixes(place_id, event_id, ts) VALUES(?,?,?)"
              " ON CONFLICT DO NOTHING", (pid, r["_event_id"], ts or 0))
    n_now = c.execute("SELECT n FROM places WHERE id=?", (pid,)).fetchone()["n"]
    _debounce(c, pid, ts or 0, acc, int(n_now))


def _debounce(c: Any, pid: int, ts: int, acc: float, count: int) -> None:
    """观测 -> 确认。**变化不一定是移动。**

    两重保险：连续 CONFIRM_N 次同向（迟滞），且那个地方本身要有证据广度
    （MIN_FIXES）。一次网络切换的跳点两条都不满足。
    """
    if count < MIN_FIXES:
        return
    # 停留（best_run）不在这里判：单点跳转已经被 CONFIRM_N 挡住，
    # 而"这算不算一个地方"是**读的时候**的事（counts_as_place）。
    # 写一个永远不会生效的判断，就是这套系统里最常见的坏法。
    row = c.execute("SELECT place_id, since, pending, pending_n FROM place_state"
                    " WHERE actor='her'").fetchone()
    if row is None:
        c.execute("INSERT INTO place_state(actor, place_id, since, pending,"
                  " pending_n) VALUES('her',?,?,NULL,0)", (pid, ts))
        return
    confirmed = row["place_id"]
    if pid == confirmed:
        c.execute("UPDATE place_state SET pending=NULL, pending_n=0"
                  " WHERE actor='her'")
        return
    if pid == row["pending"]:
        n = int(row["pending_n"]) + 1
    else:
        n = 1
    if n >= CONFIRM_N:
        c.execute("UPDATE place_state SET place_id=?, since=?, pending=NULL,"
                  " pending_n=0 WHERE actor='her'", (pid, ts))
    else:
        c.execute("UPDATE place_state SET pending=?, pending_n=?"
                  " WHERE actor='her'", (pid, n))


# ── 读 ──────────────────────────────────────────────────────


def current(db: DB, *, actor: str = "her") -> dict[str, Any] | None:
    """**确认过的**所在场所。不是最后一条读数。"""
    row = db.one("SELECT s.place_id AS id, s.since, p.lat, p.lon, p.name,"
                 " p.n, p.best_run"
                 " FROM place_state s JOIN places p ON p.id=s.place_id"
                 " WHERE s.actor=? AND p.n>=? AND p.best_dwell_ms>=?",
                 (actor, MIN_FIXES, MIN_DWELL_MS))
    return dict(row) if row else None


def all_places(db: DB) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT id, lat, lon, radius, n, name, first_ts, last_ts, best_run,"
        " best_dwell_ms"
        " FROM places WHERE scope='self:her' ORDER BY n DESC")]


def counts_as_place(p: dict) -> bool:
    """这条记录算不算"一个地方"。**两条都要：证据广度 + 停留。**"""
    return (int(p.get("n") or 0) >= MIN_FIXES
            and int(p.get("best_dwell_ms") or 0) >= MIN_DWELL_MS)


def real_places(db: DB) -> list[dict]:
    return [p for p in all_places(db) if counts_as_place(p)]


def aliases(db: DB, pid: int) -> list[str]:
    """一个地方可以有多个叫法。按（时间, 名字）排序，**确定性**。"""
    return [str(r["alias"]) for r in db.q(
        "SELECT alias FROM place_alias WHERE place_id=? ORDER BY ts, alias", (pid,))]


def name_of(p: dict | None, db: DB | None = None) -> str:
    """主叫法：最早给的那个。**不猜——没有就是空。**"""
    if not p:
        return ""
    if db is not None:
        got = aliases(db, int(p["id"]))
        if got:
            return got[0]
    return (p.get("name") or "").strip()


def name(db: DB, *, place_id: int, alias: str, src: str = "her",
         now: int) -> int | None:
    """给一个地方起名字。**落事件**，不是直接改表。

    名字是标注不是学习结果，但它同样得可追溯：谁什么时候说的、能不能重放。
    """
    alias = (alias or "").strip()
    if not alias or len(alias) > 24:
        return None
    import json

    from gits.core.db import Writer

    body = json.dumps({"place_id": int(place_id), "alias": alias, "src": src},
                      ensure_ascii=False)
    ev, created = Writer(db).append_event(
        source="self", kind="place_name", ext_id=f"place_name:{place_id}:{alias}",
        ts=now, recv_ts=now, payload=body.encode())
    if created:
        # 实时也要落投影。只写事件的话，要等到下一次 replay 才生效——
        # 中间那段时间背景块里的 loc 还是占位词。
        from gits.core import project

        row = db.one("SELECT * FROM events WHERE id=?", (ev,))
        with Writer(db).txn() as c:
            project._apply(c, row, body)
    return ev


def line(db: DB, *, now: int) -> str:
    """B 段那一行。有名字说名字，没名字退回城市（由 loc.line 负责）。"""
    p = current(db)
    n = name_of(p)
    return f"他这会儿在{n}。" if n else ""


def profile(db: DB, pid: int) -> list[int]:
    """这个场所出现在哪些小时。**给以后的 s(t|p) 用，现在只做观察。**"""
    from gits.time import bin_of

    hours = [0] * 24
    for r in db.q("SELECT ts FROM place_fixes WHERE place_id=? AND ts>0", (pid,)):
        hours[bin_of(int(r["ts"])) % 24] += 1
    return hours
