"""实例的身份。

在这之前只有"她"——一个隐含的单数。三个实例要先能**互相指认**，
才谈得上"谁对谁说了什么"。

一条规矩：**身份是数据，不是配置**。写入 `actors` 表，跟着日志一起重放；
不放进 systemd 单元。否则副本上重放出来的图会缺一个实例。
"""

from __future__ import annotations

from dataclasses import dataclass

from gits.db import DB, Writer, now_ms

MAIN = "her"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    is_main: bool


def ensure(db: DB, actor_id: str, *, is_main: bool = False,
           ts: int | None = None, note: str | None = None) -> Actor:
    with Writer(db).txn() as c:
        c.execute(
            "INSERT INTO actors(actor_id, created_ts, is_main, note)"
            " VALUES(?,?,?,?) ON CONFLICT(actor_id) DO NOTHING",
            (actor_id, ts or now_ms(), 1 if is_main else 0, note))
    row = db.one("SELECT * FROM actors WHERE actor_id=?", (actor_id,))
    assert row is not None
    return Actor(actor_id=str(row["actor_id"]), is_main=bool(row["is_main"]))


def all_of(db: DB) -> list[Actor]:
    return [Actor(actor_id=str(r["actor_id"]), is_main=bool(r["is_main"]))
            for r in db.q("SELECT * FROM actors ORDER BY is_main DESC, actor_id")]


def peers(db: DB) -> list[str]:
    """除了她以外的实例。**不含她**——她没有"peer"，她是主个体。"""
    return [a.actor_id for a in all_of(db) if not a.is_main]


def others(db: DB, actor: str) -> list[str]:
    """这个实例能找的人。**包括她**——peer 也可以找她。"""
    return [a.actor_id for a in all_of(db) if a.actor_id != actor]


def is_main(db: DB, actor: str) -> bool:
    return bool(db.scalar("SELECT is_main FROM actors WHERE actor_id=?", (actor,)))
