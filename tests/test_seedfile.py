"""初始化包：机器生成、人看不见、只解析。

测的重点不是"能不能读"，是**坏文件不许半途写进去**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gits import attention, persona, seedfile
from gits.db import DB

T0 = 1_800_000_000_000


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def _doc(**kw) -> str:
    base = {
        "format": "gits-seed/1",
        "actors": [
            {"id": "her", "main": True, "persona": "我不太会哄人。",
             "topics": {"开会": 1.2}},
        ],
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def test_合法就能过(db) -> None:
    b = seedfile.parse(_doc())
    assert [a.actor_id for a in b.actors] == ["her"]
    assert b.actors[0].topics == {"开会": 1.2}


def test_下划线开头的键是注释(db) -> None:
    doc = json.loads(_doc())
    doc["_note"] = "生成器随便写"
    doc["actors"][0]["_why"] = "随便写"
    assert seedfile.parse(json.dumps(doc, ensure_ascii=False)).actors


@pytest.mark.parametrize("patch,msg", [
    ({"format": "gits-seed/2"}, "format"),
    ({"actors": []}, "actors"),
    ({"extra": 1}, "未知字段"),
    ({"actors": [{"id": "Her", "main": True, "persona": "x"}]}, "id 不合法"),
    ({"actors": [{"id": "her", "main": True, "persona": ""}]}, "persona"),
    ({"actors": [{"id": "her", "main": True, "persona": "x",
                  "topics": {"a": 99}}]}, "超出"),
    ({"actors": [{"id": "her", "main": True, "persona": "x"},
                  {"id": "p2", "main": True, "persona": "y"}]}, "只有一个 main"),
    ({"actors": [{"id": "her", "main": False, "persona": "x"}]}, "只有一个 main"),
])
def test_不合法的整份拒绝(db, patch, msg) -> None:
    with pytest.raises(seedfile.Bad, match=msg):
        seedfile.parse(_doc(**patch))


def test_不是_json要说清位置(db) -> None:
    with pytest.raises(seedfile.Bad, match="第 1 行"):
        seedfile.parse("{")


def test_报告不回显正文(db) -> None:
    b = seedfile.parse(_doc())
    out = seedfile.describe(b)
    assert "我不太会哄人" not in out, "生成它的人本来就不该看到内容"
    assert "开会=1.2" in out and "字" in out


def test_写进去之后A段就有了(db) -> None:
    seedfile.apply(db, seedfile.parse(_doc()), now=T0)
    cur = persona.current(db)
    assert cur is not None and "我不太会哄人" in cur["narrative"]
    assert attention.weights(db, "her") == {"开会": 1.2}


def test_重导同一份不会重复(db) -> None:
    b = seedfile.parse(_doc())
    seedfile.apply(db, b, now=T0)
    seedfile.apply(db, b, now=T0 + 1)
    assert db.scalar("SELECT count(*) FROM events WHERE kind='seed'") == 1


def test_仓库里的文件不导(db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "seed.json"
    f.write_text(_doc(), encoding="utf-8")
    assert seedfile.inside_repo(f, repo=repo)
    assert not seedfile.inside_repo(tmp_path / "外.json", repo=repo)


def test_不允许伪造活动历史(db) -> None:
    """"""
    doc = json.loads(_doc())
    doc["events"] = [{"kind": "msg_in", "ts": T0}]
    with pytest.raises(seedfile.Bad, match="未知字段"):
        seedfile.parse(json.dumps(doc, ensure_ascii=False))


def test_模板文件本身合法(db) -> None:
    tpl = Path(__file__).resolve().parents[1] / "docs" / "seed-template.json"
    b = seedfile.parse(tpl.read_text(encoding="utf-8"))
    assert len(b.actors) == 3 and sum(a.main for a in b.actors) == 1
