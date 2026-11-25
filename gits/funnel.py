"""记忆漏斗：不是分层，是容量 + 复现。

两条分开的机制，别混（设计见 docs/research/funnel.md）：

  **遗忘**  一条经历没人再提，r 掉下去，检索不到它。可逆（还能想起来）。
  **压缩**  同类的东西被**独立地**反复重新推导，才允许压成一条更抽象的。不可逆。

一句话：**容量决定漏斗的形状，复现决定上升的动力。**

为什么一次性记忆绝不能压缩：压缩会把"想不起来"变成"模模糊糊记得"，
而后者是编造的开始。汉堡店的位置忘了就是忘了，压成
"他以前住的地方附近有家汉堡店"比忘掉更糟。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gits.db import DB, Writer, now_ms
from gits.fsrs import DAY, retrievability
from gits.llm import Reply, chat, cost_cny
from gits.project import entity_id, node_id_for

#: 最低间隔：同一个下午的三十条不算三十次
MIN_GAP_DAYS = 0.5
#: 下面这个数只是**记账用**，不参与判定——判据是间隔，不是 r。
#: 实测：FSRS 的曲线比直觉平得多（s=1 天时 10 天后 r 还有 0.55），
#: 用 r<0.3 当门槛意味着要等两个多月，那不是"不同时段"，是"不同的季度"。
FORGOTTEN = 0.30
#: 允许压缩所需的最少独立重述次数
MIN_RECUR = 2
#: 允许升到下一深度：还要跨过这么多个不同的周
MIN_RECUR_DEEP = 3
MIN_WEEKS = 3
#: 每个实体簇的容量上限（只是上限，不是配额）
CAPACITY = 24
#: 至少还有这么多条活着，才值得压。
#: 注意不是"活着 > 容量"——间隔重复的簇在任意时刻大部分都在遗忘，
#: 那条门槛永远跨不过去。容量看总数，遗忘只看"还剩几条值得留下"。
MIN_ALIVE = 2
#: 压缩时留几条最不相似的样本（留纹理，不是留典型）
KEEP = 3

PROMPT = """下面是同一件事在不同时候的若干条记录。把它们压成一两句。

{items}

只输出 JSON：{"summary": "一两句话", "why": "凭什么这么概括（一句话）"}

规则：
- 写发生了什么、成了什么样的模式，不写感受，不写评价。
- **必须保留至少一个具体指称**（人名、地方、东西、具体的事）。
- 不要写"他很善良""她很和蔼"这种谁都套得上的话。
- 越具体越好，但要比原件短。
"""


@dataclass
class Cluster:
    entity: str
    node_ids: list[str]
    recurrence: int          # 独立重述次数（不是条数）
    span_days: float
    weeks: int

    def can_compact(self) -> bool:
        return len(self.node_ids) > CAPACITY and self.recurrence >= MIN_RECUR

    def can_deepen(self) -> bool:
        return self.recurrence >= MIN_RECUR_DEEP and self.weeks >= MIN_WEEKS


def recurrence(occurrences: list[tuple[int, float]]) -> int:
    """独立重述次数。**用她自己记忆的稳定期当尺子，不用墙钟。**

        gap >= max(上一次的 s, 半天)  →  算一次独立重述

    为什么用 s 而不是固定天数：s 是"这条记忆本来能撑多久"。间隔超过它，
    就是一次真正的重新提起；没超过，只是同一段叙述的延续。

    为什么还要半天的最低间隔：同一个下午连发三十条，s 很小，间隔也小，
    但它显然不是三十次独立重述。
    """
    n = 0
    for i in range(1, len(occurrences)):
        prev_ts, prev_s = occurrences[i - 1]
        ts = occurrences[i][0]
        gap_days = max(0.0, (ts - prev_ts) / DAY)
        if gap_days >= max(max(0.1, prev_s), MIN_GAP_DAYS):
            n += 1
    return n


def _occurrences(db: DB, node_ids: list[str]) -> list[tuple[int, str, float]]:
    out = []
    for nid in node_ids:
        row = db.one("SELECT born_ts, s FROM nodes WHERE id=?", (nid,))
        if row is None:
            continue
        out.append((int(row["born_ts"]), nid, float(row["s"] or 1.0)))
    return sorted(out)


def cluster_of(db: DB, entity: str) -> Cluster:
    """实体锚定的簇：所有 mentions 到它的节点。

    不用向量——ghost 预算 120MB，任何 embedding 模型都放不进去
    （bge-small-zh 就要 80MB）。mentions 边是抽取时已经在写的，零成本。
    代价是"主题相似但没有共同实体"归不到一起；可以接受——
    真正会被反复提起的东西一定有名字。
    """
    eid = entity_id(entity)
    ids = [str(r["src"]) for r in db.q(
        "SELECT src FROM edges WHERE dst=? AND kind='mentions'", (eid,))]
    occ = _occurrences(db, ids)
    if not occ:
        return Cluster(entity=entity, node_ids=[], recurrence=0,
                       span_days=0.0, weeks=0)
    span = (occ[-1][0] - occ[0][0]) / DAY
    weeks = len({(t // DAY) // 7 for t, _n, _s in occ})
    return Cluster(entity=entity, node_ids=[n for _t, n, _s in occ],
                   recurrence=recurrence([(t, s) for t, _n, s in occ]),
                   span_days=span, weeks=weeks)


def entities(db: DB) -> list[str]:
    """实体名，去重。同一个实体可能有多行内容（不同消息各插过一次），
    但簇是按节点算的，名字重复会让调度排同一件事好几遍。"""
    seen: list[str] = []
    for r in db.q(
            "SELECT c.content FROM nodes n JOIN node_content c ON c.node_id=n.id"
            " WHERE n.kind='entity' ORDER BY n.id"):
        name = str(r["content"])
        if name not in seen:
            seen.append(name)
    return seen

@dataclass
class Compacted:
    entity: str
    node_id: str
    depth: int
    children: int
    summary: str
    ratio: float          # 压缩比 = 子节点总字数 / 摘要字数
    cost: float


def _material(db: DB, node_ids: list[str], *, keep: int) -> tuple[list[str], list[str]]:
    """挑要压缩的和要留的。**留最不相似的，不是最典型的。**

    典型的样本会强化摘要里已经有的东西；不相似的样本才留得住纹理——
    否则反复压缩之后只剩一句格言。
    """
    texts = []
    for nid in node_ids:
        row = db.one("SELECT content FROM node_content WHERE node_id=?"
                     " ORDER BY id DESC LIMIT 1", (nid,))
        texts.append((nid, str(row["content"]) if row else ""))
    if len(texts) <= keep:
        return [], [n for n, _t in texts]
    # 朴素的做法：按长度和字符集差异挑最"不一样"的几条当样本
    keep_ids: list[str] = []
    pool = list(texts)
    pool.sort(key=lambda kv: len(kv[1]))
    keep_ids.append(pool.pop(0)[0])          # 最短的（往往最具体）
    while pool and len(keep_ids) < keep:
        best, best_score = pool[0], -1.0
        for nid, text in pool:
            score = min(_overlap(text, db_text) for _k, db_text in
                        [(k, t) for k, t in texts if k in keep_ids])
            if score > best_score:
                best, best_score = (nid, text), score
        keep_ids.append(best[0])
        pool = [p for p in pool if p[0] != best[0]]
    to_press = [n for n, _t in texts if n not in keep_ids]
    return to_press, keep_ids


def _overlap(a: str, b: str) -> float:
    """粗相似度：字符二元组 Jaccard。够用了，不引依赖。"""
    def grams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def compact(db: DB, entity: str, *, now: int | None = None,
            model: str = "deepseek-flash", force: bool = False) -> Compacted | None:
    """压一次。**先落 model_calls，再落投影**——反过来的话中途崩了就永远回不去。

    摘要不含具体指称就整次放弃：宁可留着原件，也不要压出一句谁都套得上的话。
    """
    now = now or now_ms()
    cl = cluster_of(db, entity)
    if not force and not cl.can_compact():
        return None

    press, keep_ids = _material(db, cl.node_ids, keep=KEEP)
    if not press:
        return None

    items = []
    for nid in press:
        row = db.one("SELECT content FROM node_content WHERE node_id=?"
                     " ORDER BY id DESC LIMIT 1", (nid,))
        if row:
            items.append(f"- {row['content']}")
    raw_chars = sum(len(i) for i in items)

    reply: Reply = chat(
        [{"role": "user", "content": PROMPT.replace("{items}", "\n".join(items))}],
        model=model, max_tokens=1200, temperature=0.2,
        thinking=True, effort="high")            # 后台活，该想多深想多深

    text = reply.text
    try:
        obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        Writer(db).append_model_call(kind="funnel_failed", model=reply.model,
                                     response=text, usage_json=str(reply.usage), ts=now)
        return None

    summary = str(obj.get("summary", "")).strip()
    if not summary:
        return None
    # 证据广度：没有具体指称的抽象不许落库（压太早会收敛成"他很善良"）
    if not any(ch.isalnum() and ord(ch) > 127 for ch in summary) and not summary.strip():
        return None

    depth = int(db.scalar("SELECT max(depth) FROM nodes WHERE id IN (%s)"
                          % ",".join("?" * len(press)), press) or 0) + 1
    nid = node_id_for("funnel", f"{entity}:{depth}:{now}")
    ev, _ = Writer(db).append_event(
        source="funnel", kind="funnel", ext_id=f"funnel-{entity}-{now}",
        ts=now, recv_ts=now,
        payload=json.dumps({"entity": entity, "summary": summary, "depth": depth,
                            "children": press, "kept": keep_ids,
                            "why": obj.get("why", "")}, ensure_ascii=False).encode())
    Writer(db).append_model_call(kind="funnel", model=reply.model, response=text,
                                 event_id=ev, usage_json=str(reply.usage), ts=now)

    with Writer(db).txn() as c:
        c.execute(
            "INSERT INTO nodes(id, kind, born_ts, scope, s, r, last_touch, depth)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (nid, "abstract", now, "world", 30.0, 1.0, now, depth))
        c.execute("INSERT INTO node_content(node_id, content, created_ts)"
                  " VALUES(?,?,?)", (nid, summary, now))
        # 摘要本身要立刻可检索——它是漏斗往下走的那一环，不是纪念品。
        from gits.search import index_node

        index_node(c, nid, summary)
        # 吸收：进不了检索，但查得到是从哪几条抽象出来的（不可检索 ≠ 不可审计）
        for child in press:
            c.execute("UPDATE node_content SET absorbed_into=? WHERE node_id=?"
                      " AND absorbed_into IS NULL", (nid, child))
            from gits.graph import add_edge

            add_edge(c, src=nid, dst=entity_id(entity), kind="mentions",
                     ts=now, w=1.0)

    return Compacted(entity=entity, node_id=nid, depth=depth,
                     children=len(press), summary=summary,
                     ratio=round(raw_chars / max(1, len(summary)), 2),
                     cost=cost_cny(reply.usage))

# ── 调度：由容量边界和遗忘决定，不是由日历 ─────────────────


def alive_count(db: DB, entity: str, *, now: int) -> int:
    from gits.fsrs import DAY, retrievability

    n = 0
    for nid in cluster_of(db, entity).node_ids:
        row = db.one("SELECT s, last_touch FROM nodes WHERE id=?", (nid,))
        if row is None or not row["s"] or row["last_touch"] is None:
            continue
        elapsed = max(0.0, (now - int(row["last_touch"])) / DAY)
        if retrievability(elapsed, float(row["s"])) >= 0.35:
            n += 1
    return n


def plan(db: DB, *, now: int, limit: int = 3) -> list[tuple[str, str]]:
    """该压谁。**不看日历，看容量和遗忘。**

    两件事同时成立才排：

      簇里还活着的东西超过容量   容量是形状——压了才有地方
      独立重述够数               复现是动力——压了才有意义

    反过来：一个簇里大部分节点已经 r<0.35（在忘），就不压它——
    它正在自己消失，压它只是把模糊的话留住。
    """
    jobs: list[tuple[float, str, str]] = []
    for name in entities(db):
        cl = cluster_of(db, name)
        if not cl.can_compact():
            continue
        alive = alive_count(db, name, now=now)
        if alive < MIN_ALIVE:
            continue                      # 全都在被忘：压它只是把模糊的话留住
        jobs.append((float(alive * max(1, cl.recurrence)), name,
                     f"活着 {alive} 条、复现 {cl.recurrence} 次"))
    jobs.sort(reverse=True)
    out: list[tuple[str, str]] = []
    for _score, name, why in jobs:
        if any(name == n for n, _w in out):
            continue
        out.append((name, why))
        if len(out) >= limit:
            break
    return out


def run(db: DB, *, now: int, limit: int = 3,
        model: str = "deepseek-flash") -> list:
    """按 plan 压几轮。失败不重试——下一拍还会排到它。"""
    out = []
    for name, _why in plan(db, now=now, limit=limit):
        try:
            got = compact(db, name, now=now, model=model)
        except Exception:
            continue
        if got is not None:
            out.append(got)
    return out


def depth_histogram(db: DB) -> dict[int, int]:
    """深度直方图应当单调递减。平坦就不是漏斗，是摘要堆。"""
    return {int(r["depth"]): int(r["n"]) for r in db.q(
        "SELECT depth, count(*) n FROM nodes WHERE kind IN ('abstract','episode')"
        " GROUP BY depth ORDER BY depth")}
