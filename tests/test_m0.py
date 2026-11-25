"""M0 的四件事（impl §7）。

    1. 提交后才确认            —— ACK 之后数据必须在盘上
    2. 崩溃后重复提交只入库一次  —— ext_id 幂等键真的能去重
    3. 空库离线重放 → 相同投影   —— 恢复重放不需要再问模型
    4. 一条回复可追到叙事版本与输入

全部在合成数据上做，不联网、不调模型。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gits.db import DB, Writer, now_ms
from gits.events import export_logset, import_logset
from gits.project import MissingModelResult, dump_projection, node_id_for, restore, trace

REPO = Path(__file__).resolve().parents[1]


def _seed(db: DB, n: int = 5) -> list[int]:
    w = Writer(db)
    ids = []
    for i in range(n):
        ts = now_ms() + i * 1000
        ev_id, created = w.append_event(
            source="wechat", kind="msg_in", ext_id=f"m{i}",
            ts=ts, recv_ts=ts, payload=f"第{i}条".encode(),
        )
        assert created
        w.append_model_call(
            kind="extract", model="stub:v0", event_id=ev_id, response=f"摘要{i}",
        )
        ids.append(ev_id)
    return ids


# ── 1. 提交后才确认 ────────────────────────────────────────────
def test_ack_means_durable(tmp_path: Path) -> None:
    """ACK 之后立刻 os._exit(9)（不 close、不 flush），数据仍必须读得到。"""
    dbp = tmp_path / "g.db"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        from gits.db import DB, Writer, now_ms
        db = DB({str(dbp)!r})
        w = Writer(db)
        ev_id, created = w.append_event(
            source="wechat", kind="msg_in", ext_id="durable-1",
            ts=now_ms(), recv_ts=now_ms(), payload=b"ack then die",
        )
        print(ev_id, flush=True)          # ← ACK
        os._exit(9)                       # ← 立刻断电式退出
        """
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert r.returncode == 9, r.stderr
    ev_id = int(r.stdout.strip())

    with DB(dbp) as db:
        row = db.one("SELECT * FROM events WHERE id=?", (ev_id,))
        assert row is not None
        assert row["payload"] == b"ack then die"


# ── 2. 崩溃后重复提交只入库一次 ────────────────────────────────
def test_duplicate_submission_is_idempotent(tmp_path: Path) -> None:
    dbp = tmp_path / "g.db"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        from gits.db import DB, Writer, now_ms
        db = DB({str(dbp)!r}); w = Writer(db)
        w.append_event(source="wechat", kind="msg_in", ext_id="dup-1",
                       ts=now_ms(), recv_ts=now_ms(), payload=b"x")
        os._exit(9)
        """
    )
    subprocess.run([sys.executable, "-c", script], check=False)

    with DB(dbp) as db:
        w = Writer(db)
        ts = now_ms()
        _, created = w.append_event(
            source="wechat", kind="msg_in", ext_id="dup-1", ts=ts, recv_ts=ts,
        )
        assert created is False
        assert db.scalar(
            "SELECT count(*) FROM events WHERE source='wechat' AND ext_id='dup-1'"
        ) == 1


def test_null_ext_id_is_rejected(tmp_path: Path) -> None:
    """可空的幂等键等于没有幂等键——直接拒绝。"""
    with DB(tmp_path / "g.db") as db:
        with pytest.raises(ValueError):
            Writer(db).append_event(source="wechat", kind="msg_in", ext_id="")


# ── 3. 空库离线重放 → 相同投影 ────────────────────────────────
def test_restore_replays_from_logset_alone(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    with DB(src_path) as db:
        _seed(db, 5)
        restore(db)
        before = dump_projection(db).canonical()
        payload = export_logset(db)

    # 把线上实际传输的字节灌进一个空库
    with DB(tmp_path / "dst.db") as dst:
        import_logset(dst, json.loads(json.dumps(payload, default=lambda b: b.decode() if isinstance(b, bytes) else b)))

        # 关掉模型：任何"顺手再问一次"都会失败
        n = restore(dst)
        assert n == 5
        assert dump_projection(dst).canonical() == before


def test_restore_refuses_to_call_the_model(tmp_path: Path) -> None:
    """缺 model_calls 时必须硬失败，不能悄悄重算。"""
    with DB(tmp_path / "g.db") as db:
        w = Writer(db)
        ts = now_ms()
        w.append_event(source="wechat", kind="msg_in", ext_id="no-call",
                       ts=ts, recv_ts=ts, payload=b"x")
        with pytest.raises(MissingModelResult):
            restore(db)


def test_redaction_survives_replay(tmp_path: Path) -> None:
    """删掉的内容不能因为重放又长回来（§15.8.1）。"""
    with DB(tmp_path / "g.db") as db:
        ids = _seed(db, 3)
        w = Writer(db)
        w.redact_event(ids[0])
        assert db.scalar("SELECT payload FROM events WHERE id=?", (ids[0],)) is None

        payload = export_logset(db)

    with DB(tmp_path / "dst.db") as dst:
        import_logset(dst, json.loads(json.dumps(payload, default=bytes.decode)))
        assert dst.scalar(
            "SELECT payload FROM events WHERE source='wechat' AND ext_id='m0'"
        ) is None


# ── 4. 一条回复可追到输入 ─────────────────────────────────────
def test_trace_explains_a_reply(tmp_path: Path) -> None:
    with DB(tmp_path / "g.db") as db:
        ids = _seed(db, 2)
        restore(db)
        call_id = db.scalar("SELECT id FROM model_calls ORDER BY id LIMIT 1")
        nid = node_id_for("wechat", "m0")

        with db.conn:
            db.conn.execute(
                "INSERT INTO outbox(ts, channel, text, status) VALUES(?,?,?,?)",
                (now_ms(), "desk_audio", "你今天好安静", "swallowed"),
            )
            ob_id = db.scalar("SELECT id FROM outbox ORDER BY id DESC LIMIT 1")
            db.conn.execute(
                "INSERT INTO influence(outbox_id, node_id, w, model_call_id)"
                " VALUES(?,?,?,?)",
                (ob_id, nid, 0.82, call_id),
            )

        t = trace(db, ob_id)
        assert t["outbox"]["text"] == "你今天好安静"
        assert t["inputs"][0]["node_id"] == nid
        assert t["inputs"][0]["content"] == "摘要0"
        assert t["inputs"][0]["model_call_id"] == call_id


# ── 附带：确定性与时间可信度 ───────────────────────────────────
def test_projection_is_byte_identical_across_runs(tmp_path: Path) -> None:
    with DB(tmp_path / "a.db") as a, DB(tmp_path / "b.db") as b:
        _seed(a); _seed(b)
        restore(a); restore(b)
        assert dump_projection(a).canonical() == dump_projection(b).canonical()


def test_upload_lag_disables_temporal_training(tmp_path: Path) -> None:
    """断线补传：lag 大的事件 ts 不可用于节律训练（§14.0）。"""
    with DB(tmp_path / "g.db") as db:
        w = Writer(db)
        t0 = now_ms()
        w.append_event(source="mic", kind="acoustic", ext_id="fresh",
                       ts=t0, recv_ts=t0 + 1_000)
        w.append_event(source="mic", kind="acoustic", ext_id="backfill",
                       ts=t0 - 6 * 3600_000, recv_ts=t0)
        fresh = db.one("SELECT ts_trust FROM events WHERE ext_id='fresh'")
        late = db.one("SELECT ts_trust FROM events WHERE ext_id='backfill'")
        assert fresh["ts_trust"] == 1
        assert late["ts_trust"] == 0
