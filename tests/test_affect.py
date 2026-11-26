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


# ── 连续转移 ─────────────────────────────────────────────────


def test_强度先下来_不是直接消失(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=3, ts=T0)
    later = affect.distribution(db, "her", now=T0 + HOUR)
    # 一小时后：3 档少了一块，2 档长出来了
    assert later["cells"][("anger", 3)] < 0.5
    assert later["cells"][("anger", 2)] > 0.02


def test_会向相邻角度扩散(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=2, ts=T0)
    d = affect.distribution(db, "her", now=T0 + HOUR)
    i = affect.ANGLES.index("anger")
    left = affect.ANGLES[(i - 1) % len(affect.ANGLES)]
    right = affect.ANGLES[(i + 1) % len(affect.ANGLES)]
    assert d["cells"][(left, 2)] > d["prior"] * 1.5
    assert d["cells"][(right, 2)] > d["prior"] * 1.5


def test_对面不相邻(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=2, ts=T0)
    d = affect.distribution(db, "her", now=T0 + HOUR)
    assert d["cells"][("fear", 2)] == pytest.approx(d["prior"]), \
        "圆盘的对角是反义，一步扩散到不了那里"


def test_越激动走得越快(db) -> None:
    low = affect.evolve({("anger", 2): 1.0}, HOUR, aroused=0.0)
    high = affect.evolve({("anger", 2): 1.0}, HOUR, aroused=1.0)
    assert high[("anger", 1)] > low[("anger", 1)], "同样一小时，激动的那个走得远"


def test_流动不改变总量_只有衰减改变(db) -> None:
    cells = {("anger", 3): 1.0}
    moved = affect.evolve(cells, HOUR)
    assert sum(moved.values()) == pytest.approx(1.0, abs=1e-9), "流动守恒"
    cells2, _ = affect.advance(cells, HOUR)
    assert sum(cells2.values()) < 1.0, "衰减才让它少"


def test_加观测之前先推进_否则丢历史(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=3, ts=T0)
    # 三小时后再来一条：旧的那条该已经走过一段，而不是原样堆着
    affect.observe(db, "her", angle="joy", intensity=1, ts=T0 + 3 * HOUR)
    d = affect.distribution(db, "her", now=T0 + 3 * HOUR)
    assert d["cells"][("anger", 2)] > d["prior"], \
        "旧的愤怒该已经降档，而不是停在 3 档"
    assert d["top"] == ("joy", 1)


def test_同一时刻问两次答案一样(db) -> None:
    affect.observe(db, "her", angle="sadness", intensity=3, ts=T0)
    a = affect.distribution(db, "her", now=T0 + 2 * HOUR)
    b = affect.distribution(db, "her", now=T0 + 2 * HOUR)
    assert a["cells"] == b["cells"]


def test_读多少次都不会把状态改掉(db) -> None:
    affect.observe(db, "her", angle="anger", intensity=3, ts=T0)
    for _ in range(5):
        affect.distribution(db, "her", now=T0 + HOUR)
    # 状态是按 state_ts 存的，读只是投影，不会累积推进
    assert db.scalar("SELECT ts FROM affect_state WHERE actor='her'") == T0


def test_强度会一路降到最低档(db) -> None:
    """只挪一格的写法会让质量卡在 2 档——24 小时后还在 2 档，那是错的。"""
    moved = affect.evolve({("anger", 3): 1.0}, 24 * HOUR)
    high = sum(v for (_a, inten), v in moved.items() if inten > 1)
    # 严格为零是做不到的：Erlang 的 b·e^(−b) 在 b=16 时还有 1e-6。
    # 要说的是「基本都下来了」，不是一个数都不许剩。
    assert high < 1e-4, f"一天之后 2/3 档还留着 {high:.2g}"


def test_强度衰减是级联不是单步(db) -> None:
    half = affect.evolve({("anger", 3): 1.0}, int(0.5 * HOUR))
    assert ("anger", 2) in half, "3 → 2"
    assert half[("anger", 2)] < half[("anger", 3)], "刚开始还是 3 档为主"
