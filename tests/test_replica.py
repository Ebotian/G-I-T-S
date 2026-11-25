"""可恢复的单向副本：正常拉取、断点续传、重复拉取、旧删除、离线重放一致。

这是那个还没被验证过的架构承诺：
> 清空投影，用日志集重放，必须得到等价结果——**在另一台机器上也一样**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import replica
from gits.db import DB, Writer
from gits.events import export_logset, import_logset
from gits.project import dump_projection, restore

T0 = 1_800_000_000_000
MIN = 60_000


def _make_source(path: Path, n: int = 5) -> Path:
    with DB(path) as db:
        w = Writer(db)
        for i in range(n):
            ts = T0 + i * MIN
            ev, _ = w.append_event(source="wechat", kind="msg_in", ext_id=f"m{i}",
                                   ts=ts, recv_ts=ts, payload=f"第{i}条".encode())
            w.append_model_call(kind="extract", model="stub:v0", event_id=ev,
                                response=f"摘要{i}")
    return path


# ── 1. 正常拉取 ────────────────────────────────────────────
def test_一次拉完且再拉无事可做(tmp_path: Path) -> None:
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        r1 = replica.pull(src, rep, "replica")
        assert r1.applied["events"] == 5
        assert r1.applied["model_calls"] == 5
        r2 = replica.pull(src, rep, "replica")
        assert sum(r2.applied.values()) == 0
        assert rep.scalar("SELECT count(*) FROM events") == 5


def test_副本可以独立重放(tmp_path: Path) -> None:
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")
        assert restore(rep) == 5
        assert dump_projection(rep).canonical(stable=True)


# ── 2. 断点续传 ────────────────────────────────────────────
def test_分批拉取最终一致(tmp_path: Path) -> None:
    src_path = _make_source(tmp_path / "src.db", n=12)
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        rounds = 0
        while True:
            r = replica.pull(src, rep, "replica", limit_rows=3)
            rounds += 1
            if not any(r.applied.values()) or rounds > 20:
                break
        assert rounds > 1, "batch=3 应该拉了好几轮"
        ok, why = replica.verify(src, rep)
        assert ok, why


def test_中途失败时游标不前进(tmp_path: Path, monkeypatch) -> None:
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        real = replica.import_logset
        calls = {"n": 0}

        def boom(dst, payload, **kw):
            calls["n"] += 1
            if calls["n"] == 2:               # 第一路成功，第二路炸
                raise RuntimeError("模拟断线")
            return real(dst, payload, **kw)

        monkeypatch.setattr(replica, "import_logset", boom)
        with pytest.raises(RuntimeError):
            replica.pull(src, rep, "replica")
        monkeypatch.setattr(replica, "import_logset", real)

        # 已应用的那一路游标前进了，炸掉的那一路没前进。
        # 顺序是 events → model_calls → ...，所以第二次（model_calls）炸。
        cur = {r["stream"]: r["last_id"] for r in
               rep.q("SELECT stream, last_id FROM sync_cursor")}
        assert cur.get("events", 0) > 0, "事件先拉，应该已经落库"
        assert cur.get("model_calls", 0) == 0, "炸掉的那一路游标不该动"

        r = replica.pull(src, rep, "replica")
        assert r.applied["events"] == 0, "事件已经落过，重拉不该重复"
        ok, why = replica.verify(src, rep)
        assert ok, why


# ── 3. 重复拉取 ────────────────────────────────────────────
def test_同一批灌两次不会重复(tmp_path: Path) -> None:
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        payload = json.loads(json.dumps(export_logset(src)))
        import_logset(rep, payload)
        import_logset(rep, payload)
        assert rep.scalar("SELECT count(*) FROM events") == 5
        assert rep.scalar("SELECT count(*) FROM model_calls") == 5


def test_游标回退再拉也安全(tmp_path: Path) -> None:
    """最坏情况：游标被回退（手工改、或副本重建），再拉一遍必须幂等。"""
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")
        with rep.conn:
            rep.conn.execute("UPDATE sync_cursor SET last_id=0")
        r = replica.pull(src, rep, "replica")
        assert r.applied["events"] == 0, "重复拉取不该新增行"
        assert rep.scalar("SELECT count(*) FROM events") == 5


# ── 4. 旧事件删除 ──────────────────────────────────────────
def test_删掉老事件会传到副本(tmp_path: Path) -> None:
    src_path = _make_source(tmp_path / "src.db", n=4)
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")          # 副本先同步旧事件
        restore(rep)
        before = len(json.loads(dump_projection(rep).canonical(stable=True))["nodes"])

        Writer(src).redact_event(1)                # 源删掉一条很老的
        r = replica.pull(src, rep, "replica")
        assert r.applied["redactions"] == 1

        assert rep.scalar("SELECT payload FROM events WHERE ext_id='m0'") is None
        restore(rep)
        after = len(json.loads(dump_projection(rep).canonical(stable=True))["nodes"])
        assert after == before - 1


def test_删除流落后不影响事件流推进(tmp_path: Path) -> None:
    """游标按流分开：删除可以慢一步，事件不该被它拖住。"""
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")
        with rep.conn:                              # 人为把删除游标打回 0
            rep.conn.execute("UPDATE sync_cursor SET last_id=0 WHERE stream='redactions'")
        r = replica.pull(src, rep, "replica")
        assert r.applied["events"] == 0


# ── 5. 一致性 ──────────────────────────────────────────────
def test_verify_会指出不一致(tmp_path: Path) -> None:
    """注意：故意破坏**投影**是测不出来的——verify 会先重放，把它修好。
    要测的是**日志集**不一致（那才是真正的丢失）。"""
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")
        with rep.conn:                    # 副本少了一条日志
            rep.conn.execute("DELETE FROM model_calls WHERE event_id=1")
            rep.conn.execute("DELETE FROM events WHERE ext_id='m0'")
        ok, why = replica.verify(src, rep)
        assert not ok
        assert "不一致" in why


def test_投影被破坏会被重放修好(tmp_path: Path) -> None:
    """这是同一件事的另一面：投影是派生物，坏了就重建，不算数据丢失。"""
    src_path = _make_source(tmp_path / "src.db")
    with DB(src_path) as src, DB(tmp_path / "rep.db") as rep:
        replica.pull(src, rep, "replica")
        restore(rep)
        with rep.conn:
            rep.conn.execute("DELETE FROM node_content")
        ok, why = replica.verify(src, rep)
        assert ok, why
