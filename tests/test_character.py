"""角色事实：记录不是记忆；年龄推导；周更带机械判定。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from gits.core.db import DB, Writer
from gits.memory import character as C

# 2026-11-26 是周四
T0 = int(dt.datetime(2026, 11, 26, 15, 0).timestamp() * 1000)
DAY = 86_400_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_年龄由生日推导_每天自动变(db) -> None:
    """**年龄不写，算。** 写的话每天一条事件，而它永远是生日的函数。"""
    C.record(db, key="birth_date", value="1999-11-30", now=T0)
    assert C.age(db, now=T0) == 26          # 11-26，还没过生日
    after = T0 + 5 * DAY                    # 12-01，过了
    assert C.age(db, now=after) == 27
    assert db.scalar("SELECT count(*) FROM events WHERE kind='character'") == 1, \
        "年龄不该产生新事件"


def test_事实是记录不是记忆(db) -> None:
    C.record(db, key="height_cm", value=168, now=T0)
    from gits.core import project

    project.restore(db)
    assert C.current(db)["height_cm"] == "168"
    assert db.scalar("SELECT count(*) FROM nodes") == 0, "角色事实进了图"


def test_未知的键收不下(db) -> None:
    assert C.record(db, key="favourite_food", value="拉面", now=T0) is None
    assert C.current(db) == {}


def test_越界的变化被拒(db) -> None:
    """模型说"这周长高了 8 厘米"——**代码不认**，保留旧值。"""
    C.record(db, key="height_cm", value=168, now=T0)
    ok, why = C.bounded(db, key="height_cm", value=176, now=T0)
    assert ok is False, why
    assert "上限" in why


def test_合理的变化收下(db) -> None:
    C.record(db, key="weight_kg", value=52, now=T0)
    ok, why = C.bounded(db, key="weight_kg", value=52.4, now=T0)
    assert ok is True, why
    C.record(db, key="weight_kg", value=52.4, now=T0 + DAY)
    assert C.current(db)["weight_kg"] == "52.4"


def test_不是数字就拒(db) -> None:
    C.record(db, key="height_cm", value=168, now=T0)
    ok, why = C.bounded(db, key="height_cm", value="挺高的", now=T0)
    assert ok is False and "不是数字" in why


def test_只在周日夜里更新(db) -> None:
    sunday_night = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    thursday = T0
    assert C.due_weekly(db, now=sunday_night) is True
    assert C.due_weekly(db, now=thursday) is False
    # 跑过一次就不再跑
    C.mark_week(db, now=sunday_night)
    assert C.due_weekly(db, now=sunday_night) is False


def test_给上下文的是数据不是句子(db) -> None:
    C.record(db, key="birth_date", value="1999-11-30", now=T0)
    C.record(db, key="height_cm", value=168, now=T0)
    got = C.line(db, now=T0)
    assert '"age":26' in got
    assert '"height_cm":168' in got
    for word in ("她", "岁", "厘米"):
        assert word not in got, f"夹带了句子：{got}"


def test_上下文开头出现一次(db) -> None:
    """和解读规则一样：只在**新开上下文**时出现。"""
    from gits.speech import respond as R
    from gits.memory.persona import seed as pseed
    from gits.core import project

    pseed(db, "我不太会哄人。", now=T0)
    project.restore(db)
    C.record(db, key="birth_date", value="1999-11-30", now=T0)

    first = R._prepare(db, now=T0, actor="her", stimulus="在吗", history=[])
    body_first = first[4]
    assert "关于你自己" in body_first[-1]["content"]

    second = R._prepare(db, now=T0, actor="her", stimulus="在吗",
                        history=[("他", "在吗"), ("我", "嗯")])
    body_second = second[4]
    assert "关于你自己" not in body_second[-1]["content"], \
        "每轮都带就是白烧 token"


# ── 周更：模型提议，代码判定 ────────────────────────────────


def test_周更里越界的提议被丢掉(db, monkeypatch) -> None:
    """模型提议 8 厘米，代码只收合理的那个。"""
    from gits.speech.llm import Reply
    from gits.memory import character as C2
    from gits.ghost import Ghost

    C2.record(db, key="height_cm", value=168, now=T0)
    C2.record(db, key="weight_kg", value=52, now=T0)

    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text='{"height_cm": 176, "weight_kg": 52.4}',
                              model="stub", usage={}, latency_ms=1))
    g = Ghost(db)
    changed = g.character_weekly(sunday)
    assert "height_cm" not in changed, "越界的被收下了"
    assert "weight_kg" in changed
    assert C2.current(db)["height_cm"] == "168", "旧值应该留着"
    assert C2.current(db)["weight_kg"] == "52.4"
    # 越界要留痕，不能让"被拒"和"没提议"看起来一样
    assert db.scalar("SELECT count(*) FROM events WHERE kind='fault'") >= 1


def test_周更只跑一次(db, monkeypatch) -> None:
    from gits.speech.llm import Reply
    from gits.memory import character as C2
    from gits.ghost import Ghost

    C2.record(db, key="weight_kg", value=52, now=T0)
    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return Reply(text='{"weight_kg": 52.3}', model="stub", usage={},
                     latency_ms=1)

    monkeypatch.setattr("gits.speech.llm.chat", fake)
    g = Ghost(db)
    g.character_weekly(sunday)
    g.character_weekly(sunday + 60_000)
    assert len(calls) == 1, "同一周跑了两遍，白花钱"


def test_模型没话说就什么都不写(db, monkeypatch) -> None:
    from gits.speech.llm import Reply
    from gits.memory import character as C2
    from gits.ghost import Ghost

    C2.record(db, key="height_cm", value=168, now=T0)
    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text="{}", model="stub", usage={}, latency_ms=1))
    assert Ghost(db).character_weekly(sunday) == ""


# ── 边界按年龄：一岁和二十五岁不是一回事 ────────────────────


def _at(db: DB, *, birth: str, now_value: float) -> None:
    C.record(db, key="birth_date", value=birth, now=T0)
    C.record(db, key="height_cm", value=now_value, now=T0)


def test_一岁孩子一周长半厘米是正常的(db) -> None:
    """婴儿期一年 25cm，一周半厘米很正常。"""
    _at(db, birth="2025-11-26", now_value=75.0)      # 1 岁
    ok, why = C.bounded(db, key="height_cm", value=75.2, now=T0)
    assert ok is True, why


def test_成年人一周长半厘米就拒(db) -> None:
    """二十五岁一周长 0.6cm 不是成长，是笔误。"""
    _at(db, birth="2001-01-01", now_value=170.0)     # 25 岁
    ok, why = C.bounded(db, key="height_cm", value=170.6, now=T0)
    assert ok is False, why
    assert "上限" in why


def test_身高只能不降(db) -> None:
    """**身高单调不减。** 任何下降都是错，多少都拒。"""
    _at(db, birth="2001-01-01", now_value=170.0)
    ok, why = C.bounded(db, key="height_cm", value=169.9, now=T0)
    assert ok is False
    assert "只能不降" in why


def test_体重可以上下(db) -> None:
    """体重没有单调性，掉了也正常。"""
    C.record(db, key="weight_kg", value=60.0, now=T0)
    ok, why = C.bounded(db, key="weight_kg", value=59.4, now=T0)
    assert ok is True, why


def test_没有生日就宽一点不是拒(db) -> None:
    """不知道年龄时不该一律拒收——那会让还没填生日的新实例永远更新不了。"""
    C.record(db, key="height_cm", value=170.0, now=T0)
    ok, why = C.bounded(db, key="height_cm", value=170.3, now=T0)
    assert ok is True, why


def test_青春期一年十厘米在一周里合理(db) -> None:
    """青春期突增一年 10cm，折到一周约 0.3cm；给的理由要能看出来是按年龄算的。"""
    _at(db, birth="2013-01-01", now_value=155.0)     # 13 岁
    ok, why = C.bounded(db, key="height_cm", value=155.2, now=T0)
    assert ok is True, why
    assert "13 岁" in why, why
