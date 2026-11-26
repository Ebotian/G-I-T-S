"""上线前的三条"看得见"：备份、通道状态、抽取失败。

这三条的共同点：**它们不动的时候和坏了的时候长得一模一样。**
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core import backup
from gits.core.db import DB, Writer
from gits.io import delivery

T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
MIN = 60_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


# ── 备份 ────────────────────────────────────────────────────


def test_备份能用(tmp_path: Path) -> None:
    src = DB(tmp_path / "src.db")
    Writer(src).append_event(source="t", kind="msg_in", ext_id="x",
                             ts=T0, recv_ts=T0, payload=b"hi")
    got = backup.run(tmp_path / "src.db", dest=tmp_path / "bk", now=T0)
    assert got["ok"] and got["integrity"] == "ok" and got["events"] == 1
    ok, why = backup.verify(got["path"])
    assert ok, why
    src.close()


def test_坏的备份不算成功(tmp_path: Path) -> None:
    """**先验再存。**一份坏的备份比没有备份更危险——它让人以为有退路。"""
    src = tmp_path / "notadb.db"
    src.write_bytes("这不是数据库".encode() * 100)
    got = backup.run(src, dest=tmp_path / "bk", now=T0)
    assert got["ok"] is False


def test_只留最近几份(tmp_path: Path) -> None:
    src = DB(tmp_path / "src.db")
    for i in range(5):
        backup.run(tmp_path / "src.db", dest=tmp_path / "bk",
                   now=T0 + i * MIN, keep=3)
    files = sorted((tmp_path / "bk").glob("gits-*.db*"))
    assert len(files) == 3, f"没按 keep 剪：{[f.name for f in files]}"
    src.close()


def test_备份失败不抛(tmp_path: Path) -> None:
    """定时任务不该因为备份失败就崩，但 status 要能看到它失败。"""
    got = backup.run(tmp_path / "missing" / "nope.db",
                     dest=tmp_path / "bk", now=T0)
    assert got["ok"] is False and got.get("error")


# ── 通道状态 ────────────────────────────────────────────────


def test_没有联系人就是没有路(db) -> None:
    ok, why = delivery.channel_ready(db, now=T0)
    assert ok is False and "收件人" in why


def test_有联系人_没积压_放行(db) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO contacts(actor_id, channel, last_context_token,"
                  " last_seen, last_text) VALUES('u1','weixin','t',?, 'x')", (T0,))
    assert delivery.channel_ready(db, now=T0)[0] is True


def test_积压堵了就不放行(db) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO contacts(actor_id, channel, last_context_token,"
                  " last_seen, last_text) VALUES('u1','weixin','t',?, 'x')", (T0,))
        c.execute("INSERT INTO outbox(ts, channel, text, status)"
                  " VALUES(?,?,?,'pending')", (T0 - 45 * MIN, "weixin", "在吗"))
    ok, why = delivery.channel_ready(db, now=T0)
    assert ok is False and "积压" in why


def test_刚发出去的不算堵(db) -> None:
    with Writer(db).txn() as c:
        c.execute("INSERT INTO contacts(actor_id, channel, last_context_token,"
                  " last_seen, last_text) VALUES('u1','weixin','t',?, 'x')", (T0,))
        c.execute("INSERT INTO outbox(ts, channel, text, status)"
                  " VALUES(?,?,?,'pending')", (T0 - MIN, "weixin", "在吗"))
    assert delivery.channel_ready(db, now=T0)[0] is True


# ── 抽取失败要可见 ──────────────────────────────────────────


def test_放弃的消息数得出来(db) -> None:
    """抽了三次都失败的消息**不会再有记忆**，但以前没有任何地方说过这件事。"""
    from gits.memory import extract as EX

    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m1",
                                    ts=T0, recv_ts=T0, payload="在吗".encode())
    for _ in range(EX.MAX_TRIES):
        Writer(db).append_model_call(kind="extract_failed", model="stub",
                                     response="", event_id=ev, ts=T0)
    assert [e["id"] for e in EX.given_up(db)] == [ev]
    assert EX.pending(db) == [], "放弃的消息不该再进待抽取队列"


def test_还没到上限就还在队列里(db) -> None:
    from gits.memory import extract as EX

    ev, _ = Writer(db).append_event(source="shell", kind="msg_in", ext_id="m2",
                                    ts=T0, recv_ts=T0, payload="在吗".encode())
    Writer(db).append_model_call(kind="extract_failed", model="stub",
                                 response="", event_id=ev, ts=T0)
    assert EX.given_up(db) == []
    assert [e["id"] for e in EX.pending(db)] == [ev]


def test_完整性不通过就不留着(tmp_path: Path, monkeypatch) -> None:
    """sqlite3 在 backup 阶段就把垃圾文件挡住了，所以"验不过"这条分支
    平时走不到——把 _check 换掉才测得到。**一条走不到的分支等于没有。**"""
    src = DB(tmp_path / "src.db")
    Writer(src).append_event(source="t", kind="msg_in", ext_id="y",
                             ts=T0, recv_ts=T0, payload=b"hi")
    monkeypatch.setattr(backup, "_check", lambda path: ("corrupt", 0))
    got = backup.run(tmp_path / "src.db", dest=tmp_path / "bk", now=T0)
    assert got["ok"] is False and got["integrity"] == "corrupt"
    assert list((tmp_path / "bk").glob("gits-*")) == [], "坏副本被留在那里冒充备份"
    src.close()


def test_通道状态真的传给了gate(db, monkeypatch) -> None:
    """**这条必须验参数，不能只验「没开口」。**

    空库的 check() 本来就会因为"没有悬着的事"返回 False——那样测不出
    参数有没有传。以前 gate 里那道"没有可用的出口"永远不触发，正是因为
    没人传这个参数，而没有任何测试盯着传没传。
    """
    from gits.ghost import Ghost
    from gits.speech import initiative as I

    seen = {}

    def spy(dbref, *, now, actor="her", channel_online=True, draw=None):
        seen["channel_online"] = channel_online
        return {"allow": False, "why": "spy", "pressure": 0.0}

    monkeypatch.setattr(I, "check", spy)
    Ghost(db).initiative(T0)
    assert seen.get("channel_online") is False, "没有联系人，却没把通道状态传下去"


def test_抽取失败会留下证据(db, monkeypatch) -> None:
    """以前只写进程内存里的 last_error：重启就没了，status 也看不到。"""
    from gits.ghost import Ghost
    from gits.memory import extract as EX

    Writer(db).append_event(source="shell", kind="msg_in", ext_id="m9",
                            ts=T0, recv_ts=T0, payload=b"hi")

    def boom(*a, **k):
        raise ValueError("模型交了空")

    monkeypatch.setattr(EX, "extract", boom)
    Ghost(db).extract(T0)
    faults = db.q("SELECT payload FROM events WHERE kind='fault'")
    assert faults, "抽取失败没有留下任何痕迹"
    assert any(b"extract" in bytes(f["payload"]) for f in faults)


def test_回复失败会留下证据(db, monkeypatch) -> None:
    """**这条路真的静默过一整天。**库缺 affect_state 的那段时间，
    每条消息都在 Ghost.reply 里抛出、被吞进 last_error，
    她一个字都没回，而 status 显示一切正常（outbox=0，fault=0）。"""
    from gits.ghost import Ghost
    from gits.speech import respond as R

    def boom(*a, **k):
        raise RuntimeError("no such table: affect_state")

    monkeypatch.setattr(R, "respond_pending", boom)
    Ghost(db).reply(T0)
    faults = db.q("SELECT payload FROM events WHERE kind='fault'")
    assert faults, "回复失败没有留下任何痕迹"
    assert any(b"reply" in bytes(f["payload"]) for f in faults)
