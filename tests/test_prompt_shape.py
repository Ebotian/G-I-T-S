"""Prompts 的形状就是缓存策略。

官方规则：只有「完整匹配缓存前缀单元」才命中（docs/research/context-budget.md）。
所以下面这几条不是风格问题，是钱的问题：

  1. system 段在同一个小时里必须逐字不变
  2. 历史轮次只追加，不重写
  3. 所有易变的东西（检索结果、额度条、本轮的话）都在最后一条里
  4. 「本轮的话」只出现一次
"""

from __future__ import annotations

from pathlib import Path

from gits.context import (PersonaSeed, RelationSnapshot, TurnContext, bucket,
                          messages)

T0 = 1_800_000_000_000

SEED = PersonaSeed(narrative = "我不太会哄人。", version = 1)
SNAP = RelationSnapshot(bucket=0, summary="认识第 3 天。", hubs=("会议",), phase="醒着")


def _ctx(text: str, recalled=()) -> TurnContext:
    return TurnContext(stimulus=text, recalled=recalled)


def test_system_段不含本轮的话() -> None:
    msgs = messages(SEED, SNAP, _ctx("开会开到吐"))
    assert "开会开到吐" not in msgs[0]["content"], "塞进 system 就是把前缀打碎"
    assert "开会开到吐" in msgs[-1]["content"]


def test_本轮的话只出现一次() -> None:
    msgs = messages(SEED, SNAP, _ctx("开会开到吐"))
    assert sum(m["content"].count("开会开到吐") for m in msgs) == 1


def test_历史轮次是追加的_前缀逐字不变() -> None:
    h = (("他", "在吗"), ("我", "在。"))
    m1 = messages(SEED, SNAP, _ctx("第一句"), turns=h)
    m2 = messages(SEED, SNAP, _ctx("第二句"), turns=h + (("他", "第一句"), ("我", "嗯")))
    assert m1[0]["content"] == m2[0]["content"], "system 必须逐字不变"
    assert m2[:len(m1) - 1] == m1[:len(m1) - 1], "老轮次必须逐字留在原位"


def test_易变的都在最后一条() -> None:
    a = messages(SEED, SNAP, _ctx("同一句", recalled=("三周前他说过这个",)))
    b = messages(SEED, SNAP, _ctx("同一句", recalled=()))
    assert a[:-1] == b[:-1], "检索结果变了也不能动前面的"
    assert "三周前" in a[-1]["content"]


def test_一小时之内_B_段逐字不变() -> None:
    from gits.context import render_b

    assert render_b(SNAP) == render_b(SNAP)
    assert bucket(T0) == bucket(T0 + 59 * 60_000)

