"""初始化包：角色事实与关系前史。**导入是唯一一次能塞进一整组数字的地方。**"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits.core import seedfile

BASE = {
    "format": "gits-seed/1",
    "ephemeral": True,
    "generated_at": 1800000000000,          # 2027-01-15
    "actors": [{"id": "her", "main": True, "persona": "占位符。", "topics": {}}],
}

BODY = {"birth_date": "1999-11-30", "height_cm": 165, "weight_kg": 52,
        "bust_cm": 86, "underbust_cm": 75, "waist_cm": 62, "hip_cm": 90,
        "shoulder_cm": 38, "inseam_cm": 78, "foot_cm": 23.5}


def pack(**kw) -> str:
    d = dict(BASE)
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def test_角色事实能进来() -> None:
    b = seedfile.parse(pack(character=BODY))
    assert b.character["height_cm"] == "165"
    assert b.character["birth_date"] == "1999-11-30"


def test_不认识的键整份拒收() -> None:
    """血型不在了——它不该被导入成"角色事实"。"""
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(character={**BODY, "blood_type": "A"}))
    assert "blood_type" in str(e.value)


def test_跨字段不自洽整份拒收() -> None:
    """165/95：每个单看都不离谱，合起来不可能。**导入正是它该把守的入口。**"""
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(character={**BODY, "weight_kg": 95}))
    assert "自相矛盾" in str(e.value) and "BMI" in str(e.value)


def test_胸围小于下胸围拒收() -> None:
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(character={**BODY, "bust_cm": 70}))
    assert "自相矛盾" in str(e.value)


def test_报告不回显数值() -> None:
    """报告只说几项。数值是内容。"""
    b = seedfile.parse(pack(character=BODY))
    text = seedfile.describe(b)
    assert "角色事实 10 项" in text
    assert "165" not in text and "86" not in text


def test_前史能进来() -> None:
    b = seedfile.parse(pack(relations={"body_count": 2,
                                       "first_time": "2019-06-01"}))
    assert b.relations["body_count"] == 2
    assert isinstance(b.relations["first_time"], int)


def test_前史不收事件型字段() -> None:
    """之后发生的事只能由事件累积——在这里预置等于凭空造历史。"""
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(relations={"body_count": 1, "intimacy": 5}))
    assert "未知字段" in str(e.value)


def test_前史在未来拒收() -> None:
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(relations={"first_time": "2030-01-01"}))
    assert "未来" in str(e.value)


def test_前史早于生日拒收() -> None:
    """跨字段：生日和初次各自都合法，放一起不可能。"""
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(character=BODY,
                            relations={"first_time": "1998-01-01"}))
    assert "出生日期" in str(e.value)


def test_前史年份打错一位拒收() -> None:
    with pytest.raises(seedfile.Bad) as e:
        seedfile.parse(pack(character=BODY,
                            relations={"first_time": "2005-06-01"}))
    assert "年龄小于" in str(e.value)


def test_body_count必须是整数() -> None:
    with pytest.raises(seedfile.Bad):
        seedfile.parse(pack(relations={"body_count": 1.5}))
    with pytest.raises(seedfile.Bad):
        seedfile.parse(pack(relations={"body_count": True}))


def test_模板本身是合法的() -> None:
    """模板要是过不了自己的校验，它就是一份坏文档。"""
    text = (Path(__file__).resolve().parent.parent
            / "docs" / "seed-template.json").read_text(encoding="utf-8")
    b = seedfile.parse(text)
    assert b.ephemeral
    assert b.character and "height_cm" in b.character


def test_导入写进事件_重导不重复(tmp_path: Path) -> None:
    from gits.core.db import DB

    db = DB(tmp_path / "g.db")
    b = seedfile.parse(pack(character=BODY, relations={"body_count": 2}))
    first = seedfile.apply(db, b, now=1700000000000)
    assert first["character"] == 10 and first["relations"] == 1
    second = seedfile.apply(db, b, now=1700000000000)
    n = db.scalar("SELECT count(*) FROM events WHERE kind='character'")
    assert n == 10, f"重导多写了 {n - 10} 条"
    from gits.memory import character

    assert character.current(db)["height_cm"] == "165"
    db.close()


def test_前史和之后的事件相加(tmp_path: Path) -> None:
    from gits.core.db import DB
    from gits.memory import relations

    db = DB(tmp_path / "g.db")
    seedfile.apply(db, seedfile.parse(
        pack(character=BODY, relations={"body_count": 2})), now=1700000000000)
    assert relations.counters(db, now=1700000000000)["body_count"] == 2
    relations.record(db, action="intimacy", now=1700000000000 + 1, src="world")
    assert relations.counters(db, now=1700000000000)["body_count"] == 3
    db.close()
