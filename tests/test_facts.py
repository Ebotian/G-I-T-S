"""验证事实级生成器本身：它得**真的有那些统计规律**。

和前面一样：生成器不写测试，等于用不可信的东西验证不可信的东西。
这里每一条都对应"没有它实验就没有区分力"。
"""

from __future__ import annotations

import collections
import statistics as st

import pytest

from synth.facts import DAY, generate

FS = generate(days=90, seed=7)


def _counts(fs):
    return collections.Counter(m.fact for m in fs.mentions)


# ── 重尾 ────────────────────────────────────────────────────
def test_提及次数是重尾的() -> None:
    vals = sorted(_counts(FS).values(), reverse=True)
    assert st.pstdev(vals) / st.mean(vals) > 1.2, "CV 太低，等于均匀分布"
    assert vals[0] > 5 * st.median(vals), "头部不够长"


def test_大多数事实只提过一次() -> None:
    """没有这一条，"频次"这个变量根本不存在。"""
    vals = list(_counts(FS).values())
    once = sum(1 for v in vals if v == 1) / len(vals)
    assert 0.3 < once < 0.85, once


def test_实体分布也是偏斜的() -> None:
    per = collections.Counter(f.entity for f in FS.facts)
    vals = sorted(per.values(), reverse=True)
    assert vals[0] >= 3 * st.median(vals)


# ── 记忆的真实形状 ──────────────────────────────────────────
def test_有被取代的旧版本() -> None:
    n = sum(1 for f in FS.facts if f.superseded_by)
    assert 5 <= n <= 40, n


def test_有换措辞的重复提及() -> None:
    assert any(m.paraphrase for m in FS.mentions)


def test_有说完就没再提过的事实() -> None:
    """只提过一次的事实要占相当比例——它们才是衰减真正要处理的那些。"""
    counts = _counts(FS)
    once = [f for f in FS.facts if counts.get(f.id, 0) == 1]
    assert len(once) > 20, f"只提过一次的太少（{len(once)}），衰减没东西可区分"
    # 记下来的事实都是被说过一次的，所以不应该存在"零次"
    assert all(counts.get(f.id, 0) >= 1 for f in FS.facts)


def test_提及都在时间窗内() -> None:
    assert all(FS.start <= m.ts < FS.end for m in FS.mentions)


# ── 查询 ────────────────────────────────────────────────────
def test_查询是间接说法() -> None:
    """带上实体名，词面检索就包打天下了，语义召回测不出来。"""
    names = {e.name for e in FS.entities}
    for q in FS.queries:
        assert not any(n in q.text for n in names), q.text


def test_答案一定在候选里() -> None:
    for q in FS.queries:
        assert q.answer in q.candidates


def test_候选里没有已过期的旧版本() -> None:
    """被取代的旧事实如果在取代之后还留着，那就是版本链没生效。"""
    superseded = {f.id: f.superseded_by for f in FS.facts if f.superseded_by}
    for q in FS.queries:
        for cid in q.candidates:
            if cid in superseded:
                new_id = superseded[cid]
                assert not any(m.fact == new_id and m.ts < q.ts
                               for m in FS.mentions), \
                    f"候选里混进了已过期版本 {cid}"


def test_查询分层同时有两种() -> None:
    """一种靠"最近"、一种靠"最多"。两者都有，模型才分得出高下。"""
    kinds = collections.Counter(q.reason for q in FS.queries)
    assert kinds["recent"] > 20 and kinds["strong"] > 5, kinds


def test_最近与最多经常不一致() -> None:
    """这是整套实验的区分力所在：如果总是同一条，A/B/C 打分一样。"""
    disagree = sum(1 for q in FS.queries if q.reason == "recent")
    assert disagree / len(FS.queries) > 0.4


# ── 可复现 ──────────────────────────────────────────────────
def test_同种子必得同数据() -> None:
    a = generate(days=30, seed=3)
    b = generate(days=30, seed=3)
    assert [m.ts for m in a.mentions] == [m.ts for m in b.mentions]
    assert [f.id for f in a.facts] == [f.id for f in b.facts]


def test_换种子换数据() -> None:
    a = generate(days=30, seed=3)
    b = generate(days=30, seed=4)
    assert [m.ts for m in a.mentions] != [m.ts for m in b.mentions]
