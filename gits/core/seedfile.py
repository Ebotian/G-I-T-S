"""初始化包：一份 JSON，机器生成，人不用看见内容。

三条设计原则：

  **只解析，不入库文件本身。**导入之后状态只在事件里；文件留在仓库外面。
  **先校验，再写。**默认 dry-run；任何一条不合法就整份拒绝——不是"跳过坏的"。
  **报告不回显正文。**错误只说位置、字段、数值范围，
    因为生成它的人本来就不该看到内容。

体裁限制得很死：只允许五样东西——实例身份、自我描述、注意力偏好、
**角色事实**（年龄/身高/体重/三围）、**关系前史**（认识他之前的账）。

**不允许塞 msg_in / inner 之类的事件**：那会伪造活动历史，
而节律、遗忘、复现全是从活动历史里学出来的。一次假导入会污染一整个月。

后两样能进来，是因为它们**不伪造活动历史**：角色事实是一组可核对的数值，
前史是一条标注为"导入"的基线。它们既不产生活动，也不冒充交互。

角色事实走 `character.problems()` 的跨字段自洽检查——**导入正是它该把守的入口**，
因为这是唯一能一次塞进一整组数字的地方。
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
MAX_CHARACTER_KEYS = 16
#: 前史条数上限。它是个计数，不是历史记录——上限用来拦笔误（多打两个零）。
MAX_BODY_COUNT = 999
#: 前史时间的年代下限（岁）。**笔误拦截**：少打一位年份就是这种错。
MIN_FIRST_AGE = 12.0
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
    #: 角色事实：键 -> 文本值。键必须是 character.ALLOWED 里的。
    character: dict[str, str] = field(default_factory=dict)
    #: 关系前史。只有 body_count 和 first_time 两项——"数得出来"的部分
    #: 由事件累积，不在这里给。
    relations: dict[str, object] = field(default_factory=dict)


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
                           "facts", "character", "relations"}
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

    generated_at = top.get("generated_at")
    if generated_at is not None and (isinstance(generated_at, bool)
                                     or not isinstance(generated_at, int)):
        raise Bad("generated_at 必须是毫秒时间戳整数")
    stamp = int(generated_at) if generated_at is not None else 0

    ch = _parse_character(top.get("character") or {})
    rel = _parse_relations(top.get("relations") or {}, generated_at=stamp)
    _check_history(ch, rel, generated_at=stamp)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return Bundle(actors=actors, digest=digest,
                  ephemeral=bool(top.get("ephemeral")), facts=facts,
                  character=ch, relations=rel)


def _parse_character(raw) -> dict[str, str]:
    """角色事实：年龄/身高/体重/三围。**键必须在白名单里，数值必须自洽。**

    这是导入器里唯一能一次塞进一整组数字的地方，所以跨字段检查必须在这里跑——
    character.bounded() 管的是"一周变了多少"，导入没有"上一周"，跑不了。
    """
    from gits.memory import character

    if not isinstance(raw, dict):
        raise Bad("character 必须是对象")
    raw = _strip_underscore(raw)
    if len(raw) > MAX_CHARACTER_KEYS:
        raise Bad(f"character 太多键（上限 {MAX_CHARACTER_KEYS}）")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k not in character.ALLOWED:
            # **不列出合法键**：报告不回显正文，键名也算半个正文。
            raise Bad(f"character 里有不认识的键：{k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise Bad(f"character.{k} 只能是数字或字符串")
        out[k] = str(v).strip()
        if not out[k]:
            raise Bad(f"character.{k} 是空的")
    bad = character.problems(out, now=0)
    if bad:
        raise Bad("character 自相矛盾：" + "；".join(bad))
    return out


def _parse_relations(raw, *, generated_at: int) -> dict[str, object]:
    """关系前史：认识他之前的那部分账。

    **只收"数不出来"的两项**——body_count（人）和 first_time（那时还没有事件）。
    之后发生的事由 kind='relation' 事件累积，不接受在这里预置：
    那等于凭空造一段没发生过的历史。
    """
    if not isinstance(raw, dict):
        raise Bad("relations 必须是对象")
    raw = _strip_underscore(raw)
    unknown = set(raw) - {"body_count", "first_time"}
    if unknown:
        raise Bad(f"relations 有未知字段：{sorted(unknown)}")
    out: dict[str, object] = {}

    n = raw.get("body_count", 0)
    if isinstance(n, bool) or not isinstance(n, int):
        raise Bad("relations.body_count 必须是整数")
    if not (0 <= n <= MAX_BODY_COUNT):
        raise Bad(f"relations.body_count 超出 [0, {MAX_BODY_COUNT}]")
    out["body_count"] = n

    ft = raw.get("first_time")
    if ft is not None:
        ms = _parse_ts(ft)
        if ms is None:
            raise Bad("relations.first_time 要 YYYY-MM-DD 或毫秒时间戳")
        if ms > generated_at:
            raise Bad("relations.first_time 在未来")
        out["first_time"] = ms
    return out


def _parse_ts(v) -> int | None:
    """'YYYY-MM-DD' 或毫秒时间戳 -> 毫秒。**只认阳历，和 facts 同一个口径。**"""
    import datetime as _dt

    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if not isinstance(v, str):
        return None
    try:
        d = _dt.datetime.strptime(v.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return int(d.timestamp() * 1000)


def _check_history(ch: dict[str, str], rel: dict[str, object], *,
                   generated_at: int) -> None:
    """前史和生日必须对得上。**跨字段，单看每一份文件都合法。**"""
    ft = rel.get("first_time")
    if ft is None:
        return
    born = ch.get("birth_date")
    if not born:
        return
    born_ms = _parse_ts(born)
    if born_ms is None:
        raise Bad("character.birth_date 要 YYYY-MM-DD")
    if ft <= born_ms:
        raise Bad("relations.first_time 早于或等于出生日期")
    # 笔误拦截，不是道德线：多打/少打一位年份是最常见的错法。
    if ft - born_ms < MIN_FIRST_AGE * 365 * 86_400_000:
        raise Bad(f"relations.first_time 对应的年龄小于 {MIN_FIRST_AGE:g} 岁，"
                  "多半是年份打错了")


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
    if b.character:
        # **只报数量和键名以外的东西吗？** 键名是字段名（height_cm），不是内容，
        # 但数值是内容——报告只说几个键。
        out.append(f"  角色事实 {len(b.character)} 项")
    if b.relations:
        out.append("  关系前史 1 段")
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
    counts = {"actors": 0, "persona": 0, "topics": 0, "facts": 0,
              "character": 0, "relations": 0}
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
    if b.character:
        from gits.memory import character

        for k, v in b.character.items():
            # src='seed'：来源与节奏表里 manual 的键只认"明确给出"，
            # 而导入就是明确给出——不是周更"总结"出来的。
            if character.record(db, key=k, value=v, src="seed", now=now):
                counts["character"] += 1
    if b.relations:
        from gits.memory import relations

        relations.seed(db, now=now,
                       body_count=int(b.relations.get("body_count") or 0),
                       first_time=b.relations.get("first_time"))
        counts["relations"] = 1
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
        "SELECT count(*) FROM nodes WHERE kind IN ('self','persona')"
        " AND scope LIKE 'self:%'") or 0
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
