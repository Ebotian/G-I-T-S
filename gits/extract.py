"""抽取：把一条消息变成图上的一小块。

DESIGN §3.2：`events` 是唯一真相，图谱是投影。所以这里做两件事，必须分开：

    调模型   产出**可重放的文本**（JSON 字符串），落进 model_calls
    落投影   只读那份文本，确定性地产出节点和边

分成两半的理由：`project.restore` 要能在**不调用任何模型**的前提下，
用同一份文本把图重建出来。抽取逻辑一旦藏在"调模型"里，重放就不等价了。

抽取是后台活，没有人在等，所以 **thinking 开着**——它该想多深想多深。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from gits.db import DB, Writer, now_ms
from gits.llm import Reply, chat, cost_cny
from gits import project
from gits.project import node_id_for

PROMPT = """把这句话拆成结构化的事实。只输出 JSON，不要解释。

{"summary": "一句话说清这件事是什么，第三人称，30 字以内",
 "entities": [{"name": "具体名词，原话里的叫法", "kind": "person|topic|thing|place"}],
 "claims": [{"subject": "谁", "predicate": "做了什么/是什么", "object": "对象"}]}

规则：
- summary 写事实，不写感受，不写建议。原文说的是"开会开到吐"，就写"他今天开会很久"。
- 只抽原话里真的出现过的。没有就留空数组，不要补。
- 人称用"他"指说话的人。

原话：{text}"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass
class Extracted:
    event_id: int
    summary: str
    node_id: str
    entities: int
    claims: int
    latency_ms: int
    cost: float


def parse(raw: str) -> dict:
    """模型偶尔会包一层 markdown 代码块。**宽松地读，严格地校验。**"""
    m = _JSON_RE.search(raw)
    if not m:
        raise ValueError(f"抽取结果里没有 JSON：{raw[:120]!r}")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict) or not str(obj.get("summary", "")).strip():
        raise ValueError(f"缺 summary：{raw[:120]!r}")
    obj.setdefault("entities", [])
    obj.setdefault("claims", [])
    return obj


def entity_id(name: str) -> str:
    """实体的稳定 id。同名同 id——这是跨消息合并的唯一依据。"""
    import hashlib

    h = hashlib.sha256(("entity\x00" + name.strip()).encode()).hexdigest()[:16]
    return f"e:{h}"


def apply(db: DB, event: dict, payload: dict, *, ts: int | None = None) -> str:
    """只读 payload，确定性落投影。返回节点 id。

    **和 project.restore 走同一个函数**（apply_payload）——两边各写一份的话，
    重放出来的图会悄悄少一批节点，而这事要等真正的灾难恢复才会被发现。
    """
    row = dict(event)
    row["ts"] = ts or event["ts"]
    with Writer(db).txn() as c:
        return project.apply_payload(c, row, payload)


def done(db: DB, event_id: int) -> bool:
    return db.one("SELECT 1 AS x FROM model_calls WHERE kind='extract'"
                  " AND event_id=? LIMIT 1", (event_id,)) is not None


#: 一条消息最多抽几次。失败会留下证据（extract_failed），但不能
#: 无限重试——一条抽不出来的消息不该让她每两秒烧一次钱。
MAX_TRIES = 3


def pending(db: DB, *, limit: int = 5) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT e.* FROM events e WHERE e.kind IN ('msg_in','inner')"
        " AND NOT EXISTS (SELECT 1 FROM model_calls m WHERE m.kind='extract'"
        "                 AND m.event_id=e.id)"
        " AND (SELECT count(*) FROM model_calls m2 WHERE m2.kind='extract_failed'"
        "      AND m2.event_id=e.id) < ?"
        " ORDER BY e.id LIMIT ?", (MAX_TRIES, limit))]


def extract(db: DB, event: dict, *, model: str = "deepseek-flash",
            thinking: bool = True, effort: str = "high",
            ts: int | None = None) -> Extracted:
    """抽一条。**先落 model_calls，再落投影**——反过来的话，中途崩了就会
    在重放时报 MissingModelResult，而那条消息再也抽不出来了。"""
    text = (event["payload"] or b"").decode("utf-8", "replace")
    msgs = [{"role": "user", "content": PROMPT.replace("{text}", text)}]
    # max_tokens 要留够：思考 token 也算 completion token。实测 800 时
    # 一次 thinking=high 把 800 全烧在 reasoning 上，可见输出是空字符串。
    reply: Reply = chat(msgs, model=model, max_tokens=2000, temperature=0.0,
                        thinking=thinking, effort=effort)

    raw = reply.text
    try:
        payload = parse(raw)
    except ValueError:
        # 抽不出来**不落 extract**：落了它，这条消息就永远"已完成"了。
        # 但也得留证据——原始输出才是"为什么抽不出来"的唯一线索。
        Writer(db).append_model_call(
            kind="extract_failed", model=reply.model, response=raw,
            event_id=event["id"], usage_json=str(reply.usage), ts=ts)
        raise

    Writer(db).append_model_call(
        kind="extract", model=reply.model, response=raw,
        event_id=event["id"], usage_json=str(reply.usage),
        prompt_hash=None, ts=ts)
    nid = apply(db, event, payload, ts=ts)
    return Extracted(
        event_id=event["id"], summary=payload["summary"], node_id=nid,
        entities=len(payload.get("entities", [])),
        claims=len(payload.get("claims", [])),
        latency_ms=reply.latency_ms, cost=cost_cny(reply.usage))
