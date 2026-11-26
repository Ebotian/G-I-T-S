"""情绪：极坐标网格上的分布，不是心情词。

要盯的三件事：

  **不是标签**  一个词会把噪声伪装成结论；分布不会
  **会散掉**    三天前的气应该散了，τ=6 小时
  **说得出杂**  低浓度时它该承认"说不太清"，而不是硬挑一格
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gits import affect
from gits.db import DB

T0 = 1_800_000_000_000
HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture()
def db(tmp_path: Path):
    d = DB(tmp_path / "g.db")
    yield d
    d.close()


def test_没观测时什么都不说(db) -> None:
    assert affect.line(db, "her", now=T0) == ""
    d = affect.distribution(db, "her", now=T0)
    assert d["n"] == 0 and d["top"] is None


def test_一次观测就有一格(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=2, ts=T0)
    d = affect.distribution(db, "her", now=T0)
    assert d["top"] == ("anger", 2)
    assert "生气" in affect.line(db, "her", now=T0)


def test_分布是概率_而且包含先验(db) -> None:
    affect.observe(db, "her", angle="joy", intensity=1, ts=T0)
    d = affect.distribution(db, "her", now=T0)
    assert len(d["cells"]) == 24, "24 格都该在，没观测的也有先验质量"
    total = sum(d["cells"].values()) + d["unknown"]
    assert total == pytest.approx(1.0, abs=1e-6)


def test_情绪会散(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=3, ts=T0)
    assert affect.line(db, "her", now=T0) != ""
    # τ=6 小时，三天后几乎没了
    assert affect.distribution(db, "her", now=T0 + 3 * DAY)["n"] < 0.01
    assert affect.line(db, "her", now=T0 + 3 * DAY) == ""


def test_同一时刻问两次答案一样(db) -> None:
    affect.observe(db, "her", angle="sadness", intensity=1, ts=T0)
    a = affect.distribution(db, "her", now=T0 + HOUR)
    b = affect.distribution(db, "her", now=T0 + HOUR)
    assert a["cells"] == b["cells"]


def test_不认识的格子进_unknown_不猜(db) -> None:
    affect.observe(db, "her", angle="委屈", intensity=2, ts=T0)
    d = affect.distribution(db, "her", now=T0)
    assert d["n"] == 0, "委屈、心酸、憋屈在 Plutchik 里没有干净的位置"
    assert d["unknown"] > 0.2


def test_杂的时候要承认杂(db) -> None:
    for angle in ("joy", "sadness", "anger", "fear", "trust"):
        affect.observe(db, "her", angle=angle, intensity=1, ts=T0)
    line = affect.line(db, "her", now=T0)
    assert "杂" in line or "说不太清" in line, f"五个方向都在，不能说得很确定：{line}"


def test_集中时说得干脆(db) -> None:
    for i in range(12):
        affect.observe(db, "her", angle="anger", intensity=2, ts=T0 + i)
    assert affect.line(db, "her", now=T0 + 12) == "最近偏生气。"
