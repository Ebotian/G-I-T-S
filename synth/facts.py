"""事实级合成数据：让"记忆"这件事有可验证的分布。

之前那版生成器只造消息：1019 条里 64 种文本、CV 1.24、单次出现 6.2%——**大致均匀**。
拿它测遗忘曲线，A/B/C 三个模型会得一样的分：所有事实只出现一次，`touches` 恒为 0，
三个公式都退化成"按年龄衰减"。那不是"没通过"，是**没有区分力**。

这一版造的是**事实**。分布的三个来源：

    实体    Yule–Simon：新实体以恒定速率进入，旧实体按已有事实数吸引 → Zipf
    提及    非齐次泊松，强度 = 实体热度（指数衰减 + 偶发复燃）× 事实显著性
    查询    按当前热度采样实体，用**间接说法**提问（带实体名就测不出语义召回）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

HOUR = 3_600_000
DAY = 86_400_000

PHRASES = ("老是出问题", "跟我较劲", "卡了半天", "又崩了", "不太对劲", "慢得要死")
INDIRECT = (
    "上次那个跟你较劲的事",
    "前阵子你说的那个",
    "那个老是出问题的家伙",
    "你之前提过一次的那个",
    "还有那个来着",
)
PREDICATES = ("状态", "计划", "偏好", "抱怨")
VALUES = ("卡住了", "想换掉", "其实还行", "太贵", "没时间", "下个月再说")


@dataclass
class Entity:
    id: str
    name: str
    born: int
    pulls: int = 1              # 作为 Yule–Simon 的吸引度


@dataclass(frozen=True)
class Fact:
    id: str
    entity: str
    predicate: str
    value: str
    born: int
    superseded_by: str | None = None
    salience: float = 1.0


@dataclass(frozen=True)
class Mention:
    fact: str
    ts: int
    paraphrase: bool = False


@dataclass(frozen=True)
class Query:
    ts: int
    entity: str
    text: str
    answer: str
    candidates: tuple[str, ...]
    reason: str = "recent"      # 'recent' | 'strong'


@dataclass
class Facts:
    start: int
    end: int
    entities: list[Entity] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def mentions_of(self, fact_id: str) -> list[int]:
        return sorted(m.ts for m in self.mentions if m.fact == fact_id)

    def facts_of(self, entity_id: str) -> list[Fact]:
        return [f for f in self.facts if f.entity == entity_id]

    def truth(self) -> dict:
        return {
            "start": self.start, "end": self.end, "params": self.params,
            "entities": [e.__dict__ for e in self.entities],
            "facts": [f.__dict__ for f in self.facts],
            "mentions": [m.__dict__ for m in self.mentions],
            "queries": [{**q.__dict__, "candidates": list(q.candidates)}
                        for q in self.queries],
        }


def generate(*, days: int = 90, seed: int = 7, start: int = 1_800_000_000_000,
             n_facts: int = 200, new_entity_per_fact: float = 0.11,
             contradiction_rate: float = 0.08,
             paraphrase_rate: float = 0.15,
             n_queries: int = 120) -> Facts:
    rng = np.random.default_rng(seed)
    end = start + days * DAY
    fs = Facts(start=start, end=end)
    names = [f"物{i:02d}" for i in range(600)]

    def new_entity(t: int) -> Entity:
        e = Entity(id=f"e{len(fs.entities):03d}", name=names[len(fs.entities)], born=t)
        fs.entities.append(e)
        return e

    for _ in range(8):
        new_entity(start)

    # ── 事实 + 提及 ──
    for _ in range(n_facts):
        if rng.random() < new_entity_per_fact:
            ent = new_entity(int(rng.integers(start, end)))
        else:
            w = np.array([e.pulls for e in fs.entities], dtype=float)
            ent = fs.entities[int(rng.choice(len(fs.entities), p=w / w.sum()))]

        born = int(max(ent.born, rng.integers(ent.born, end)))
        # 显著性本身是重尾：多数事实说一次就完了，少数反复出现
        # 显著性重尾：多数事实说一次就完了，少数反复出现——CV 靠的就是这一项
        sal = float(min(np.random.default_rng(seed + len(fs.facts)).lognormal(0.0, 1.3), 40.0))
        fid = f"f{len(fs.facts):04d}"
        fs.facts.append(Fact(
            id=fid, entity=ent.id, predicate=str(rng.choice(PREDICATES)),
            value=str(rng.choice(VALUES)), born=born, salience=sal,
        ))
        ent.pulls += 1

        # ★ 提及是**分段的**，不是均匀的泊松：那阵子天天提，然后销声匿迹，
        # 隔很久又说起一次。没有这个簇结构，"间隔效应"就没有东西可以利用——
        # 实测第一版（纯泊松）里 decays 打不过"只看最近一次"，原因就在这。
        n_episodes = 1 + int(rng.poisson(max(0.2, sal / 3.0)))
        t = born
        for _ep in range(min(n_episodes, 12)):
            length = 1 + int(rng.poisson(min(sal, 6.0)))
            for _m in range(min(length, 8)):
                if t >= end:
                    break
                fs.mentions.append(Mention(fact=fid, ts=t,
                                           paraphrase=bool(rng.random() < paraphrase_rate)))
                # 簇内：几小时到两三天一次
                t = int(t + float(rng.exponential(1.2 * DAY)))
            # 簇间：几周到几个月，热度衰减
            gap = float(rng.exponential(30 * DAY / max(sal, 0.5)))
            t = int(t + max(gap, 3 * DAY))

    # ── 矛盾：取值被后来的相反值取代（版本链） ──
    for f in list(fs.facts):
        if rng.random() >= contradiction_rate:
            continue
        seen = [m.ts for m in fs.mentions if m.fact == f.id]
        if not seen:
            continue
        t = int(max(seen) + rng.exponential(7 * DAY))
        if t >= end:
            continue
        new = Fact(id=f"f{len(fs.facts):04d}", entity=f.entity,
                   predicate=f.predicate, value=str(rng.choice(VALUES)),
                   born=t, salience=f.salience)
        fs.facts.append(new)
        fs.facts[fs.facts.index(f)] = Fact(**{**f.__dict__, "superseded_by": new.id})
        fs.mentions.append(Mention(fact=new.id, ts=t))

    fs.mentions.sort(key=lambda m: m.ts)

    # ── 查询 ──
    by_id = {f.id: f for f in fs.facts}
    for _ in range(n_queries):
        qt = int(rng.integers(start + 10 * DAY, end))
        recent = [m for m in fs.mentions if qt - 14 * DAY <= m.ts < qt]
        if not recent:
            continue
        fact = by_id[recent[int(rng.integers(len(recent)))].fact]

        cands = [f for f in fs.facts_of(fact.entity) if f.born < qt]
        # 已被取代的旧版本，若在 qt 之前就已经被取代，就不该再出现在候选里
        alive = []
        for f in cands:
            if f.superseded_by and any(
                    m.fact == f.superseded_by and m.ts < qt for m in fs.mentions):
                continue
            alive.append(f)
        if len(alive) < 2:
            continue

        last = {f.id: max([m.ts for m in fs.mentions if m.fact == f.id and m.ts < qt],
                          default=-1) for f in alive}
        answer = max(last, key=last.get)
        if last[answer] < 0:
            continue

        cnt = {f.id: len([m for m in fs.mentions
                          if m.fact == f.id and m.ts < qt]) for f in alive}
        reason = "strong" if max(cnt, key=cnt.get) == answer else "recent"
        fs.queries.append(Query(
            ts=qt, entity=fact.entity,
            text=f"{INDIRECT[int(rng.integers(len(INDIRECT)))]}，"
                 f"{PHRASES[int(rng.integers(len(PHRASES)))]}",
            answer=answer, candidates=tuple(f.id for f in alive), reason=reason,
        ))

    fs.params = {
        "days": days, "seed": seed, "n_entities": len(fs.entities),
        "n_facts": len(fs.facts), "n_mentions": len(fs.mentions),
        "n_queries": len(fs.queries),
        "contradiction_rate": contradiction_rate,
        "paraphrase_rate": paraphrase_rate,
    }
    return fs
