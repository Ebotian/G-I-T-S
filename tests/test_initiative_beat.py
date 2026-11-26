"""心跳不再用固定节拍跑主动开口。**排期由时序模型给。**"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB
from gits.ghost import Ghost

HOUR = 3_600_000
T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_跑过一次之后才排下一次(db) -> None:
    """**没有它就是在每拍都判一次**——判定便宜，可它一旦放行就是一次模型调用。"""
    g = Ghost(db)
    g.initiative(T0)
    assert g._next_initiative > T0, "跑完之后没有排下一次"


def test_排期在将来_不会每拍都跑(db) -> None:
    g = Ghost(db)
    g.initiative(T0)
    nxt = g._next_initiative
    assert nxt - T0 >= 60_000, "排期太近，等于没排"
    # 把钟往回拨：还没到点
    assert t_before(g, nxt - 1) is False


def t_before(g: Ghost, t: int) -> bool:
    return t >= g._next_initiative


def test_没到点心跳就不碰它(db, monkeypatch) -> None:
    """真的走一遍 tick：这是唯一能证明"固定节拍确实没了"的方式。"""
    g = Ghost(db)
    g.initiative(T0)
    g._next_initiative = T0 + HOUR
    called = []
    monkeypatch.setattr(Ghost, "initiative",
                        lambda self, t=None: called.append(t) or "")
    g.tick(T0 + 1)                 # 没到点
    assert called == [], "没到点就跑了"
    g.tick(T0 + HOUR + 1)          # 到点
    assert called == [T0 + HOUR + 1], called
