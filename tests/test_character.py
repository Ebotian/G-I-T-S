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


# ── 哪些事实会变、怎么变 ────────────────────────────────────


def test_周更只写可测量的那几个(db, monkeypatch) -> None:
    """模型塞进来的野键（"blood_type"）没有白名单，不收。"""
    from gits.speech.llm import Reply
    from gits.ghost import Ghost

    C.record(db, key="weight_kg", value=52, now=T0)
    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text='{"blood_type": "B", "weight_kg": 52.2}',
                              model="stub", usage={}, latency_ms=1))
    changed = Ghost(db).character_weekly(sunday)
    assert changed == "weight_kg", changed
    assert "blood_type" not in C.current(db)


def test_每种事实有自己的更新节奏() -> None:
    """不同状态不同时序——写在表里，不散在 if 里。"""
    assert C.CADENCE["height_cm"] == "weekly"
    assert C.CADENCE["birth_date"] == "manual"
    assert "bust_cm" in C.WEEKLY_KEYS
    assert "birth_date" not in C.WEEKLY_KEYS
    for key in C.ALLOWED:
        assert key in C.CADENCE, f"{key} 没有说明多久更新一次"


def test_年龄不存_所以不在节奏表里(db) -> None:
    assert "age" not in C.CADENCE and "age" not in C.ALLOWED
    C.record(db, key="birth_date", value="1999-11-30", now=T0)
    assert C.age(db, now=T0) == 26


# ── 三围：可测量的女性身体结构 ──────────────────────────────


def test_三围在允许的键里() -> None:
    for k in ("bust_cm", "waist_cm", "hip_cm", "underbust_cm"):
        assert k in C.ALLOWED, k
        assert k in C.WEEKLY_KEYS, f"{k} 应该能周更"


def test_三围有年龄相关的上限(db) -> None:
    """青春期在发育，成年后只随体重浮动——同一个 2cm，含义不同。"""
    C.record(db, key="birth_date", value="2013-01-01", now=T0)   # 13 岁
    C.record(db, key="bust_cm", value=80.0, now=T0)
    ok, why = C.bounded(db, key="bust_cm", value=81.0, now=T0)
    assert ok is True, why
    C.record(db, key="bust_cm", value=81.0, now=T0)
    ok, why = C.bounded(db, key="bust_cm", value=84.0, now=T0)
    assert ok is False, why


def test_三围不是数字就拒(db) -> None:
    C.record(db, key="waist_cm", value=62.0, now=T0)
    ok, why = C.bounded(db, key="waist_cm", value="细", now=T0)
    assert ok is False and "不是数字" in why


def test_三围进头块(db) -> None:
    C.record(db, key="bust_cm", value=86.0, now=T0)
    C.record(db, key="waist_cm", value=62.0, now=T0)
    C.record(db, key="hip_cm", value=90.0, now=T0)
    got = C.line(db, now=T0)
    assert '"bust_cm":86' in got and '"waist_cm":62' in got and '"hip_cm":90' in got


def test_周更不许改生日(db, monkeypatch) -> None:
    """生日是年龄的源。模型"总结"一下就能改，那年龄就不可信了。"""
    from gits.speech.llm import Reply
    from gits.ghost import Ghost

    C.record(db, key="birth_date", value="1999-11-30", now=T0)
    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text='{"birth_date": "2001-01-01", "weight_kg": 52.2}',
                              model="stub", usage={}, latency_ms=1))
    changed = Ghost(db).character_weekly(sunday)
    assert "birth_date" not in changed
    assert C.current(db)["birth_date"] == "1999-11-30"
    assert C.age(db, now=T0) == 26, "年龄跟着生日一起变了"


# ── 派生量与自洽检查 ────────────────────────────────────────


def _body(db: DB, **kw) -> None:
    for k, v in kw.items():
        C.record(db, key=k.replace("_v", ""), value=v, now=T0)


def test_派生量算得出来(db) -> None:
    _body(db, birth_date="1999-11-30", height_cm=165, weight_kg=52,
          bust_cm=86, underbust_cm=75, waist_cm=62, hip_cm=90,
          inseam_cm=78, foot_cm=23.5)
    d = C.derived(db, now=T0)
    assert d["bmi"] == 19.1
    assert d["cup"] == "B", d           # 86-75 = 11 -> B
    assert d["band"] == 75
    assert d["whr"] == pytest.approx(0.689, abs=0.01)
    assert d["leg_ratio"] == pytest.approx(0.473, abs=0.01)
    assert d["shoe_cn"] == 37.0


def test_派生量不落库(db) -> None:
    _body(db, height_cm=165, weight_kg=52)
    before = db.scalar("SELECT count(*) FROM events WHERE kind='character'")
    C.derived(db, now=T0)
    after = db.scalar("SELECT count(*) FROM events WHERE kind='character'")
    assert before == after, "派生量写进库了"


def test_自洽的身材没有意见(db) -> None:
    _body(db, height_cm=165, weight_kg=52, bust_cm=86, underbust_cm=75,
          waist_cm=62, hip_cm=90, shoulder_cm=38, inseam_cm=78)
    assert C.problems(now=T0, db=db) == []


def test_胸围小于下胸围会被指出(db) -> None:
    _body(db, bust_cm=80, underbust_cm=85)
    assert any("互换" in p for p in C.problems(now=T0, db=db))


def test_腰围大于臀围会被指出(db) -> None:
    _body(db, waist_cm=95, hip_cm=90)
    assert any("不小于臀围" in p for p in C.problems(now=T0, db=db))


def test_BMI_不合理会被指出(db) -> None:
    """身高 165、体重 95 —— 每个单看都不离谱，合起来不可能。"""
    _body(db, height_cm=165, weight_kg=95)
    assert any("BMI" in p for p in C.problems(now=T0, db=db))


def test_肩宽与身高不匹配会被指出(db) -> None:
    _body(db, height_cm=165, shoulder_cm=50)
    assert any("肩宽" in p for p in C.problems(now=T0, db=db))


def test_腿身比不合理会被指出(db) -> None:
    _body(db, height_cm=165, inseam_cm=60)
    assert any("内缝长" in p for p in C.problems(now=T0, db=db))


def test_周更会跑自洽检查(db, monkeypatch) -> None:
    """**这条只能验到"检查被调用"，验不到规则本身。**

    原因值得记：每字段的周上限是 ±1.5cm 量级，所以**一步之内几乎不可能**
    由合法的单字段变化凑出一个跨字段矛盾。跨字段检查真正守的是
    一次塞进一整组的入口——导入和手工写入。"""
    from gits.speech.llm import Reply
    from gits.ghost import Ghost
    from gits.memory import character as C2

    _body(db, birth_date="1999-11-30", height_cm=165, weight_kg=52)
    sunday = int(dt.datetime(2026, 11, 29, 21, 0).timestamp() * 1000)
    called = []
    monkeypatch.setattr(C2, "problems",
                        lambda *a, **k: called.append(1) or ["假的矛盾"])
    monkeypatch.setattr(
        "gits.speech.llm.chat",
        lambda *a, **k: Reply(text='{"weight_kg": 52.3}', model="stub",
                              usage={}, latency_ms=1))
    changed = Ghost(db).character_weekly(sunday)
    assert called, "周更没有跑自洽检查"
    assert changed == "", "检查说有矛盾，还是收下了"
    assert C2.current(db)["weight_kg"] == "52"


def test_手工写入不自洽就拒(db) -> None:
    """一次塞进一整组的入口：粘贴、导入。这里才是跨字段检查的用武之地。"""
    from gits.cli import cmd_character
    import argparse

    C.record(db, key="bust_cm", value=80, now=T0)
    args = argparse.Namespace(db=str(db.path), key="underbust_cm",
                              value="85")
    assert cmd_character(args) == 2
    assert "underbust_cm" not in C.current(db)


def test_胸围差超出范围会被指出(db) -> None:
    """胸围大于下胸围，但差得离谱——这两个数不是互换，是其中一个错。"""
    _body(db, bust_cm=110, underbust_cm=70)      # 差 40，超过 30 的包络
    assert any("胸围差" in p for p in C.problems(now=T0, db=db))


def test_腰臀比超出范围会被指出(db) -> None:
    """腰比臀小，但比例不可能——同样是"每个都像，合起来不像"。"""
    _body(db, waist_cm=55, hip_cm=92)            # 0.598，低于 0.62
    assert any("腰臀比" in p for p in C.problems(now=T0, db=db))


def test_头块里带上派生量(db) -> None:
    """她知道自己 75B，比知道自己胸围 86 更自然。"""
    _body(db, height_cm=165, weight_kg=52, bust_cm=86, underbust_cm=75,
          waist_cm=62, hip_cm=90, foot_cm=23.5)
    got = C.line(db, now=T0)
    assert '"cup":"B"' in got, got
    assert '"bmi":19.1' in got, got
    assert '"shoe_cn":37' in got or '"shoe_cn":37.0' in got, got


# ── 统一口径：会变的 ≤1cm，成年后不变 ───────────────────────


def test_成长期围度最多变一厘米(db) -> None:
    C.record(db, key="birth_date", value="2013-01-01", now=T0)      # 13 岁
    C.record(db, key="bust_cm", value=80.0, now=T0)
    ok, why = C.bounded(db, key="bust_cm", value=81.0, now=T0)
    assert ok is True, why
    ok, why = C.bounded(db, key="bust_cm", value=81.1, now=T0)
    assert ok is False, why
    assert "上限 1.00" in why


def test_成年后围度认为不变(db) -> None:
    """25 岁还能改胸围，那不是生活变化，是数据错。"""
    C.record(db, key="birth_date", value="2001-01-01", now=T0)
    C.record(db, key="waist_cm", value=62.0, now=T0)
    ok, why = C.bounded(db, key="waist_cm", value=62.5, now=T0)
    assert ok is False, why
    assert "上限 0.00" in why


def test_成年后身高认为不变(db) -> None:
    C.record(db, key="birth_date", value="2001-01-01", now=T0)
    C.record(db, key="height_cm", value=165.0, now=T0)
    ok, why = C.bounded(db, key="height_cm", value=165.5, now=T0)
    assert ok is False, why


def test_成长期身高也最多一厘米(db) -> None:
    C.record(db, key="birth_date", value="2013-01-01", now=T0)      # 13 岁
    C.record(db, key="height_cm", value=155.0, now=T0)
    ok, _ = C.bounded(db, key="height_cm", value=156.0, now=T0)
    assert ok is True
    ok, why = C.bounded(db, key="height_cm", value=156.2, now=T0)
    assert ok is False, why


def test_成年后连降也不许(db) -> None:
    """单调仍然生效——不变就是不变，两个方向都不行。"""
    C.record(db, key="birth_date", value="2001-01-01", now=T0)
    C.record(db, key="height_cm", value=165.0, now=T0)
    ok, why = C.bounded(db, key="height_cm", value=164.5, now=T0)
    assert ok is False
    assert "只能不降" in why or "上限 0.00" in why
