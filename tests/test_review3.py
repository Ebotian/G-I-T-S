"""第三轮评审的回归测试。

每一条都对应一个**被复现出来的** bug，不是假想的：
  - export 直接 dump bytes → TypeError
  - say 分两次提交，中间崩溃 → 永远补不上抽取结果
  - 导入用本地自增 id 引用，两库顺序不同就串了
  - 删除流按被删事件的 id 推进游标 → 删老事件永远传不到副本
  - 删除只清 payload → 重放从抽取响应里把正文读回来
  - 调度只看计划周期，不看实际新鲜度
  - GLM 把断线当成"零活动"
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
from gits.events import export_logset, import_logset, iter_stream
from gits.gate import GateInputs
from gits.project import dump_projection, restore
from gits.sched import Health, Node, Task, eligible
from gits.sync import plan

REPO = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000_000
HOUR = 3_600_000
MIN = 60_000


def _seed(db: DB, pairs, *, start: int = T0) -> None:
    """按给定顺序写事件，每条都配一份抽取结果。"""
    w = Writer(db)
    for i, ext in enumerate(pairs):
        ts = start + i * MIN
        ev_id, _ = w.append_event(source="wechat", kind="msg_in", ext_id=ext,
                                  ts=ts, recv_ts=ts, payload=f"正文-{ext}".encode())
        w.append_model_call(kind="extract", model="stub:v0", event_id=ev_id,
                            response=f"摘要-{ext}")


# ── 1. 导出必须能过 JSON ────────────────────────────────────
def test_export_是_json_安全的(tmp_path: Path) -> None:
    with DB(tmp_path / "g.db") as db:
        _seed(db, ["a", "b"])
        s = json.dumps(export_logset(db))          # 原来这里 TypeError
        assert "__b64__" in s


def test_cli_全链路(tmp_path: Path) -> None:
    """say → export → import → replay，用真的命令行入口。"""
    a, b, wire = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "w.json"

    def cli(db: Path, *args: str, stdin: str | None = None):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        return subprocess.run(
            [sys.executable, "-m", "gits.cli", "--db", str(db), *args],
            capture_output=True, text=True, env=env, input=stdin,
        )

    assert cli(a, "say", "今天开会开到想吐").returncode == 0
    assert cli(a, "say", "你还在吗").returncode == 0
    r = cli(a, "export")
    assert r.returncode == 0, r.stderr
    wire.write_text(r.stdout, encoding="utf-8")

    r = cli(b, "import", stdin=wire.read_text())
    assert r.returncode == 0, r.stderr
    counts = json.loads(r.stdout)
    assert counts["events"] == 2 and counts["model_calls"] == 2

    r = cli(b, "replay", "--restore")
    assert r.returncode == 0, r.stderr
    assert "replayed 2 events" in r.stdout


def test_say_崩溃后重试能补上() -> None:
    """同一 ext_id 重试时，事件已存在但结果缺失——必须补，而不是跳过。"""
    code = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        from gits.db import DB, Writer, now_ms
        db = DB(sys.argv[1]); w = Writer(db)
        # 只写事件、不写结果，然后崩
        w.append_event(source="wechat", kind="msg_in", ext_id="half",
                       ts={T0}, recv_ts={T0}, payload=b"x")
        os._exit(9)
    """)
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "g.db"
    r = subprocess.run([sys.executable, "-c", code, str(tmp)], capture_output=True,
                       text=True)
    assert r.returncode == 9

    env = dict(os.environ, PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-m", "gits.cli", "--db", str(tmp),
                        "say", "补上", "--ext-id", "half"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    with DB(tmp) as db:
        n = db.scalar("SELECT count(*) FROM model_calls")
        assert n == 1, "重试没有补上抽取结果，恢复重放会失败"
        restore(db)


# ── 2. 两库合并：引用必须跟着稳定身份 ──────────────────────
def test_两库合并引用不串(tmp_path: Path) -> None:
    """同一批事件，两边写入顺序不同 → 数字 id 不同 → 引用必须按 (source, ext_id) 重绑。"""
    left, right = tmp_path / "l.db", tmp_path / "r.db"
    with DB(left) as db:
        _seed(db, ["x", "y", "z"])
        payload = json.loads(json.dumps(export_logset(db)))
    with DB(right) as db:
        _seed(db, ["z", "x", "y"])          # 顺序不同
        import_logset(db, payload)
        restore(db)
        rows = db.q("SELECT e.ext_id, m.response FROM events e"
                    " JOIN model_calls m ON m.event_id = e.id ORDER BY e.ext_id")
        got = {r["ext_id"]: r["response"] for r in rows}
        assert got == {"x": "摘要-x", "y": "摘要-y", "z": "摘要-z"}


# ── 3. 删除：游标、传播、重放 ──────────────────────────────
def test_删老事件不会被游标漏掉(tmp_path: Path) -> None:
    """游标的参照物必须是**删除流自己的序号**，不是被删事件的 id。

    原来的实现按被删事件 id 推进：删一条 id=1 的老事件时，游标早就到 3 了，
    `WHERE event_id > 3` 永远看不到它——删除静默失效。
    """
    with DB(tmp_path / "g.db") as db:
        w = Writer(db)
        _seed(db, ["a", "b", "c", "d"])
        w.redact_event(1)                    # 先删一条
        cur = [r["id"] for r in iter_stream(db, "redactions", 0)][-1]

        w.redact_event(2)                    # 再删一条**更老**的
        seen = list(iter_stream(db, "redactions", cur))
        assert [r["event_id"] for r in seen] == [2], (
            f"删老事件被游标漏掉了：{seen}"
        )


def test_删除会传到副本并清掉结果(tmp_path: Path) -> None:
    with DB(tmp_path / "g.db") as db:
        _seed(db, ["a", "b"])
        Writer(db).redact_event(1)
        payload = json.loads(json.dumps(export_logset(db)))
        assert db.scalar("SELECT response FROM model_calls WHERE event_id=1") is None

    with DB(tmp_path / "r.db") as rep:
        import_logset(rep, payload)
        assert rep.scalar("SELECT payload FROM events WHERE ext_id='a'") is None
        assert rep.scalar("SELECT response FROM model_calls WHERE event_id="
                          "(SELECT id FROM events WHERE ext_id='a')") is None


def test_删除后重放不再生成那个节点(tmp_path: Path) -> None:
    with DB(tmp_path / "g.db") as db:
        _seed(db, ["a", "b"])
        restore(db)
        before = json.loads(dump_projection(db).canonical())["nodes"]
        Writer(db).redact_event(1)
        restore(db)
        after = json.loads(dump_projection(db).canonical())["nodes"]
        assert len(after) == len(before) - 1


# ── 4. 调度看实际新鲜度 ────────────────────────────────────
def test_计划周期达标但实际落后也要拒绝() -> None:
    node = Node(id="office", cpu=12, ram_mb=30000, ram_budget_mb=8000,
                resident_mb=600, disk_free_gb=600, sync_period_ms=5 * MIN)
    task = Task(kind="x", ram_mb=100, est_ms=1000, staleness_max_ms=10 * MIN)
    assert eligible(node, task, Health(1000, 2 * MIN))[0]
    ok, why = eligible(node, task, Health(1000, 40 * MIN))
    assert not ok and "没更新" in why


def test_删除流永不被跳过() -> None:
    plans = {p.stream: p for p in plan("office", sync_period_ms=30 * MIN,
                                       freshness_ms=0, pending_up={})}
    assert plans["redactions"].action == "push"
    assert plans["self_narrative"].action == "skip"


# ── 5. 断线不能被学成低谷 ──────────────────────────────────
def test_可观测窗口让断线不再是低谷() -> None:
    import numpy as np
    from gits.presence import fit

    # 断线的占比越大，偏差越明显——这是"把没数据当零"的直接后果。
    # 26/60 天不可观测时，naive 的基线率会被压掉三成以上。
    rng = np.random.default_rng(0)
    events = []
    gap = (T0 + 17 * 24 * HOUR, T0 + 43 * 24 * HOUR)     # 中间断线 26 天
    for h in range(60 * 24):
        t = T0 + h * HOUR
        if gap[0] <= t < gap[1]:
            continue
        if rng.random() < 0.6:                          # 不分昼夜的强度
            events.append(t + int(rng.random() * HOUR))
    observed = [(T0, gap[0]), (gap[1], T0 + 60 * 24 * HOUR)]

    naive = fit(events)
    fair = fit(events, observed=observed)
    mid = (gap[0] + gap[1]) // 2
    assert fair.rate(mid) > naive.rate(mid) * 1.3, (
        f"断线仍被学成低谷：naive={naive.rate(mid):.2f} fair={fair.rate(mid):.2f}"
    )
