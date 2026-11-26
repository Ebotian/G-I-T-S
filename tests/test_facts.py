"""固定事实：生日这类东西。

测的重点是**它不该被衰减碰**，以及**它平时不该出现**。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.speech import facts
from gits.core import project
from gits.core.db import DB, Writer

DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _ts(y: int, m: int, d: int) -> int:
    """某个本地日期的中午，换成毫秒时间戳。"""
    return int(dt.datetime(y, m, d, 12, tzinfo=dt.timezone(
        dt.timedelta(hours=facts.TZ_OFFSET_HOURS))).timestamp() * 1000)


def test_平时不出现(db) -> None:
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=3, day=12, lead_days=3, now=_ts(2027, 1, 1))
    assert facts.lines(db, _ts(2027, 1, 1)) == ()
    assert facts.due(db, _ts(2027, 2, 28)) == []


def test_窗口内才出现(db) -> None:
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=3, day=12, lead_days=3, now=_ts(2027, 1, 1))
    assert facts.lines(db, _ts(2027, 3, 9)) == ("3 天后是他的生日。",)
    assert facts.lines(db, _ts(2027, 3, 11)) == ("明天是他的生日。",)
    assert facts.lines(db, _ts(2027, 3, 12)) == ("今天是他的生日。",)
    assert facts.lines(db, _ts(2027, 3, 13)) == (), "过了就退出去"


def test_只给事实不给建议(db) -> None:
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=3, day=12, now=_ts(2027, 1, 1))
    line = facts.lines(db, _ts(2027, 3, 12))[0]
    for word in ("祝", "应该", "记得", "别忘了"):
        assert word not in line, f"这是原料，不是脚本：{line}"


def test_不衰减也不进节点(db) -> None:
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=3, day=12, now=_ts(2027, 1, 1))
    assert db.scalar("SELECT count(*) FROM nodes") == 0, \
        "事实不是记忆，不该进图——否则 364 天后 r≈0，正好在那天被忘掉"
    far = _ts(2037, 3, 12)
    assert facts.lines(db, far) == ("今天是他的生日。",), "十年后照样想得起来"


def test_改一条是顶掉不是覆盖(db) -> None:
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=3, day=12, now=_ts(2027, 1, 1))
    facts.add(db, subject="him", key="birthday", label="他的生日",
              month=4, day="1" if False else 1, now=_ts(2027, 2, 1))
    live = db.q("SELECT month FROM facts WHERE superseded_by IS NULL")
    assert [r["month"] for r in live] == [4]
    assert db.scalar("SELECT count(*) FROM facts") == 2, "旧的那条还在，只是被顶掉"


def test_二月二十九在平年按三月一算(db) -> None:
    assert facts.days_until(2, 29, dt.date(2026, 3, 1)) == 0
    assert facts.days_until(2, 29, dt.date(2026, 2, 28)) == 1


def test_能重放(db) -> None:
    ev = facts.add(db, subject="him", key="birthday", label="他的生日",
                   month=3, day=12, lead_days=3, year=1990,
                   now=_ts(2027, 1, 1))
    assert ev > 0
    project.restore(db)
    row = db.one("SELECT label, month, day, year, lead_days FROM facts")
    assert row["month"] == 3 and row["year"] == 1990 and row["lead_days"] == 3


def test_一次性事实过了就不再来(db) -> None:
    facts.add(db, subject="him", key="deadline", label="截止",
              month=3, day=12, year=2027, repeat="once",
              lead_days=2, now=_ts(2027, 1, 1))
    assert facts.lines(db, _ts(2027, 3, 10)) == ("2 天后是截止。",)
    assert facts.lines(db, _ts(2027, 3, 13)) == ()
    assert facts.lines(db, _ts(2028, 3, 12)) == (), "once 不该每年重复"
