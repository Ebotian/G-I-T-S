"""初始化包：一份 JSON，机器生成，人不用看见内容。

三条设计原则：

  **只解析，不入库文件本身。**导入之后状态只在事件里；文件留在仓库外面。
  **先校验，再写。**默认 dry-run；任何一条不合法就整份拒绝——不是"跳过坏的"。
  **报告不回显正文。**错误只说位置、字段、数值范围，
    因为生成它的人本来就不该看到内容。

体裁限制得很死：只允许三样东西——实例身份、自我描述、注意力偏好。
**不允许塞 msg_in / inner 之类的事件**：那会伪造活动历史，
而节律、遗忘、复现全是从活动历史里学出来的。一次假导入会污染一整个月。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "gits-seed/1"
MAX_FACTS = 64
MAX_LEAD_DAYS = 30
MAX_LABEL = 40
#: 日历只支持阳历。lunar 明确拒绝——**猜错一次会在错误的那天祝人，比忘记更糟**。
CALENDARS = {"solar"}
MAX_CHARS = 2000          # 一段自我描述
MAX_ACTORS = 8
MAX_TOPICS = 32
MAX_FILE = 64 * 1024
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
TOPIC_RE = re.compile(r"^[^\x00-\x1f]{1,24}$")
WEIGHT_RANGE = (0.1, 5.0)


class Bad(Exception):
    """整份拒绝。**不跳过、不猜。**"""


@dataclass
class ActorSeed:
    actor_id: str
    main: bool
    persona: str
    topics: dict[str, float] = field(default_factory=dict)


@dataclass
class FactSeed:
    subject: str
    key: str
    label: str
    month: int
    day: int
    year: int | None = None
    lead_days: int = 0
    repeat: str = "yearly"


@dataclass
class Bundle:
    actors: list[ActorSeed]
    digest: str          # 整份内容的哈希：幂等键，也用来回答"这是哪份文件"
    #: 测试/示例包。生产导入器默认拒绝——模板里的词一旦进了生产库，
    #: 会和真实交互共振并被永久强化。
    ephemeral: bool = False
    facts: list[FactSeed] = field(default_factory=list)


def _strip_underscore(d: dict) -> dict:
    """以 _ 开头的键当注释，忽略。生成器可以随便加注释。"""
    return {k: v for k, v in d.items() if not str(k).startswith("_")}


def parse(text: str) -> Bundle:
    if len(text.encode("utf-8")) > MAX_FILE:
        raise Bad(f"文件太大（上限 {MAX_FILE // 1024} KB）")
    if "\x00" in text:
        raise Bad("文件里有 NUL 字节")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Bad(f"不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    if not isinstance(raw, dict):
        raise Bad("顶层必须是对象")

    top = _strip_underscore(raw)
    got = top.get("format")
    if got != FORMAT:
        raise Bad(f"format 必须是 {FORMAT}")
    unknown = set(top) - {"format", "generated_at", "actors", "ephemeral",
                           "facts"}
    if unknown:
        raise Bad(f"顶层有未知字段：{sorted(unknown)}")

    actors_raw = top.get("actors")
    if not isinstance(actors_raw, list) or not actors_raw:
        raise Bad("actors 必须是非空数组")
    if len(actors_raw) > MAX_ACTORS:
        raise Bad(f"实例太多（上限 {MAX_ACTORS}）")

    actors: list[ActorSeed] = []
    seen: set[str] = set()
    for i, a in enumerate(actors_raw):
        where = f"actors[{i}]"
        if not isinstance(a, dict):
            raise Bad(f"{where} 必须是对象")
        a = _strip_underscore(a)
        extra = set(a) - {"id", "main", "persona", "topics"}
        if extra:
            raise Bad(f"{where} 有未知字段：{sorted(extra)}")
        aid = a.get("id")
        if not isinstance(aid, str) or not ID_RE.match(aid):
            raise Bad(f"{where}.id 不合法（小写字母开头，2-32 位）")
        if aid in seen:
            raise Bad(f"{where}.id 重复：{aid}")
        seen.add(aid)

        persona = a.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            raise Bad(f"{where}.persona 必须是非空字符串")
        if len(persona) > MAX_CHARS:
            raise Bad(f"{where}.persona 太长（{len(persona)} 字，上限 {MAX_CHARS}）")

        topics_raw = a.get("topics") or {}
        if not isinstance(topics_raw, dict):
            raise Bad(f"{where}.topics 必须是对象")
        if len(topics_raw) > MAX_TOPICS:
            raise Bad(f"{where}.topics 太多（上限 {MAX_TOPICS}）")
        topics: dict[str, float] = {}
        for k, v in topics_raw.items():
            if not isinstance(k, str) or not TOPIC_RE.match(k):
                raise Bad(f"{where}.topics 里有非法话题名（第 {len(topics) + 1} 个）")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise Bad(f"{where}.topics 权重必须是数字")
            lo, hi = WEIGHT_RANGE
            if not (lo <= float(v) <= hi):
                raise Bad(f"{where}.topics 有一项权重 {v} 超出 [{lo}, {hi}]")
            topics[k] = float(v)

        actors.append(ActorSeed(actor_id=aid, main=bool(a.get("main")),
                                persona=persona.strip(), topics=topics))

    mains = [a.actor_id for a in actors if a.main]
    if len(mains) != 1:
        raise Bad(f"必须有且只有一个 main，现在是 {len(mains)} 个")

    facts = _parse_facts(top.get("facts") or [], seen)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return Bundle(actors=actors, digest=digest,
                  ephemeral=bool(top.get("ephemeral")), facts=facts)


def describe(b: Bundle, *, inspect: bool = False) -> str:
    """校验报告。**默认连话题名都不给。**

    话题名也是内容：生成脚本随机抽到「深海热泉」「编译原理」，
    跑一次 dry-run 就把核心关注点全剧透了——那 blind 就白设了。
    默认只给数量与权重和；要看名字得显式 --inspect。
    """
    head = f"格式 ok（{FORMAT}），摘要 {b.digest}"
    if b.ephemeral:
        head += "  [ephemeral：示例包，生产默认拒收]"
    out = [head, f"实例 {len(b.actors)} 个："]
    if b.facts:
        leads = sum(1 for f in b.facts if f.lead_days > 0)
        out.append(f"  固定事实 {len(b.facts)} 条"
                   f"（{leads} 条带提前窗口，{len(b.facts) - leads} 条当天）")
    for a in b.actors:
        n = len(a.topics)
        if not n:
            weights = "偏好 无"
        elif inspect:
            weights = "偏好 " + "、".join(
                f"{k}={v:g}" for k, v in sorted(a.topics.items()))
        else:
            weights = f"偏好 {n} 项（权重和 {sum(a.topics.values()):g}）"
        out.append(f"  {a.actor_id}"
                   f"{'（主）' if a.main else ''}"
                   f"  自我描述 {len(a.persona)} 字  {weights}")
    return "\n".join(out)


def inside_repo(path: Path, repo: Path = Path("/srv/gits")) -> bool:
    """文件不能放在仓库里——她读不到仓库，但仓库会被 push 出去。"""
    try:
        path.resolve().relative_to(repo.resolve())
        return True
    except ValueError:
        return False


def apply(db, b: Bundle, *, now: int | None = None) -> dict[str, int]:
    """写进事件。幂等：ext_id 里带整份内容的哈希，重导同一份不会重复。"""
    from gits.social import actors
    from gits.social import attention
    from gits.speech import facts
    from gits.core.db import Writer, now_ms

    now = now or now_ms()
    counts = {"actors": 0, "persona": 0, "topics": 0, "facts": 0}
    for a in b.actors:
        actors.ensure(db, a.actor_id, is_main=a.main, ts=now)
        counts["actors"] += 1
        Writer(db).append_event(
            source="seed", kind="seed", ext_id=f"seed-{a.actor_id}-{b.digest}",
            ts=now, recv_ts=now, scope=f"self:{a.actor_id}",
            payload=a.persona.encode())
        counts["persona"] += 1
        if a.topics:
            counts["topics"] += attention.seed(db, a.actor_id, a.topics, ts=now)
    for f in b.facts:
        facts.add(db, subject=f.subject, key=f.key, label=f.label,
                  month=f.month, day=f.day, year=f.year,
                  lead_days=f.lead_days, repeat=f.repeat, now=now)
        counts["facts"] += 1
    return counts


def projection_gap(db) -> int:
    """种子事件有多少条还没进投影。**大于 0 就说明 A 段还是空的。**

    导入写的是事件，A 段读的是投影节点——这两步之间要跑一次 replay。
    不提示的话，使用者会以为导入没生效。
    """
    events = db.scalar(
        "SELECT count(*) FROM events WHERE kind IN ('seed','persona')"
        " AND payload IS NOT NULL"
        " AND id NOT IN (SELECT event_id FROM redactions)") or 0
    nodes = db.scalar(
        "SELECT count(*) FROM nodes WHERE kind='self' AND scope LIKE 'self:%'") or 0
    return max(0, int(events) - int(nodes))


def _parse_facts(raw, actor_ids: set[str]) -> list[FactSeed]:
    """固定事实。**只支持阳历**——lunar 明确拒绝，不猜。

    猜错一次会在错误的那天祝人，那比忘记更糟。
    """
    if not isinstance(raw, list):
        raise Bad("facts 必须是数组")
    if len(raw) > MAX_FACTS:
        raise Bad(f"facts 太多（上限 {MAX_FACTS}）")
    out: list[FactSeed] = []
    for i, f in enumerate(raw):
        where = f"facts[{i}]"
        if not isinstance(f, dict):
            raise Bad(f"{where} 必须是对象")
        f = _strip_underscore(f)
        extra = set(f) - {"subject", "key", "label", "month", "day",
                          "year", "lead_days", "repeat", "calendar"}
        if extra:
            raise Bad(f"{where} 有未知字段：{sorted(extra)}")
        cal = f.get("calendar", "solar")
        if cal not in CALENDARS:
            raise Bad(f"{where}.calendar = {cal!r} 不支持（只有 solar）")
        subject = f.get("subject")
        if not isinstance(subject, str) or not ID_RE.match(subject):
            raise Bad(f"{where}.subject 不合法")
        item = f.get("key")
        if not isinstance(item, str) or not ID_RE.match(item):
            raise Bad(f"{where}.key 不合法")
        label = f.get("label")
        if not isinstance(label, str) or not label.strip():
            raise Bad(f"{where}.label 必须是非空字符串")
        if len(label) > MAX_LABEL:
            raise Bad(f"{where}.label 太长（上限 {MAX_LABEL} 字）")
        month, day = f.get("month"), f.get("day")
        if not isinstance(month, int) or not 1 <= month <= 12:
            raise Bad(f"{where}.month 必须是 1..12")
        if not isinstance(day, int) or not 1 <= day <= 31:
            raise Bad(f"{where}.day 必须是 1..31")
        from gits.speech.facts import _this_year_date

        try:
            _this_year_date(2000, month, day)
        except ValueError:
            raise Bad(f"{where} 的 {month}/{day} 不是合法日期") from None
        year = f.get("year")
        if year is not None and (not isinstance(year, int)
                                 or not 1800 <= year <= 2200):
            raise Bad(f"{where}.year 必须是 1800..2200 或省略")
        lead = f.get("lead_days", 0)
        if not isinstance(lead, int) or not 0 <= lead <= MAX_LEAD_DAYS:
            raise Bad(f"{where}.lead_days 必须是 0..{MAX_LEAD_DAYS}")
        repeat = f.get("repeat", "yearly")
        if repeat not in ("yearly", "once"):
            raise Bad(f"{where}.repeat 只能是 yearly / once")
        out.append(FactSeed(subject=subject, key=str(item),
                            label=label.strip(), month=month, day=day,
                            year=year, lead_days=lead, repeat=repeat))
    return out
