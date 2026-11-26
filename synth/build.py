"""把场景写成真实的 events：走正规写入路径，不是伪造的表。

走正规路径的意义：合成数据会经过 ext_id 幂等、ts/recv_ts、lag、
ts_trust 这一整套，于是**验证的就是线上会跑的那套代码**。

断线补传是刻意模拟的：断线期间产生的事件先压在本地，重连时统一上传，
于是 recv_ts 全挤在重连那一刻、lag 很大、ts_trust=0——这正是 §14.0
要求"补传数据退出节律训练"的场景。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from gits.core.db import DB, Writer
from synth.dialogue import THOUGHTS, fill_messages
from synth.schedule import AT_DESK, DAY, OFFLINE, Scenario, generate

STEP_MS = 10 * 60_000       # 声学采样间隔
NODE = "office"


def _eid(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:20]


@dataclass
class Built:
    db_path: Path
    truth_path: Path
    events: int
    calls: int


def build(
    out_dir: str | Path,
    *,
    scenario: str = "regular",
    days: int = 30,
    seed: int = 7,
    corpus: list[tuple[str, str]] | None = None,
    durable: bool = False,
) -> Built:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "gits.db"
    for p in out.glob("gits.db*"):
        p.unlink()

    sc = generate(scenario, days=days, seed=seed)
    rng = random.Random(seed * 31 + 7)
    fill_messages(sc, rng, corpus=corpus)

    db = DB(db_path, durable=durable)
    w = Writer(db)

    # ── 1. 收集事件。断线期间产生的先压在本地队列里。 ──
    pending: list[dict] = []
    rows: list[dict] = []
    obs: list[tuple[str, int, int, str]] = []

    def emit(ts: int, source: str, kind: str, ext_id: str, payload: bytes | None = None) -> None:
        rows.append({"ts": ts, "recv_ts": ts, "source": source, "kind": kind,
                     "ext_id": ext_id, "payload": payload})

    for seg in sc.segments:
        # 通道状态：OFFLINE 段 = 她看不见
        obs.append(("wechat" if seg.state != OFFLINE else "all",
                    seg.start, seg.end, "down" if seg.state == OFFLINE else "up"))

        t = seg.start
        while t < seg.end:
            if seg.state == AT_DESK:
                ev = {"ts": t, "source": "mic", "kind": "acoustic",
                      "ext_id": _eid(NODE, "acoustic", t),
                      "payload": b"\x00" * 8}
                (pending if any(o[3] == "down" and o[1] <= t < o[2] for o in obs) else rows).append(ev)
            t += STEP_MS

        if seg.state != OFFLINE:
            emit(seg.start, "mic", "node", _eid(NODE, "node", seg.start, seg.state),
                 seg.state.encode())

    # 消息：断线期间同样先压住
    for ts, text in sc.messages:
        ev = {"ts": ts, "source": "wechat", "kind": "msg_in",
              "ext_id": _eid("wechat", ts, text), "payload": text.encode()}
        if any(o[3] == "down" and o[1] <= ts < o[2] for o in obs):
            pending.append(ev)
        else:
            rows.append(ev)

    for i, ts in enumerate(sc.inner_thoughts):
        rows.append({"ts": ts, "source": "self", "kind": "inner",
                     "ext_id": _eid("self", "inner", ts),
                     "payload": THOUGHTS[i % len(THOUGHTS)].encode()})

    # ── 2. 断线结束的时刻：补传，recv_ts 全部挤在这一刻 ──
    gap_end = None
    for _, start, end, state in obs:
        if state == "down":
            gap_end = end
    if gap_end is not None:
        for ev in pending:
            rows.append({**ev, "recv_ts": gap_end})

    rows.sort(key=lambda r: r["ts"])
    n_events = w.append_events_bulk(rows)

    # ── 3. 每条会进投影的事件都配一份"已保存的模型结果" ──
    # 恢复重放不重新调用模型（§3.1），所以这里必须补齐。
    n_calls = 0
    for r in db.q("SELECT id, payload FROM events WHERE kind IN ('msg_in','inner')"):
        text = (r["payload"] or b"").decode(errors="replace")
        w.append_model_call(kind="extract", model="stub:v0", event_id=r["id"],
                            response=text[:40])
        n_calls += 1

    for channel, start, end, state in obs:
        w.set_observability(channel, start, state, end)

    truth_path = out / "truth.json"
    truth_path.write_text(json.dumps(sc.truth(), ensure_ascii=False, indent=1),
                          encoding="utf-8")
    db.close()
    return Built(db_path=db_path, truth_path=truth_path, events=n_events, calls=n_calls)
