"""工具循环：让她能在回话中途查一次东西。

**只给一次机会。** 一轮对话里最多调一个工具、最多多一个来回——
再多就不是"查一下"，是"在后台跑任务"，而对方在等着回话。

三条约束，每条都有理由：

  ① 一次调用就够      每次调用都是一次完整的上下文重发（12K 预算下 ≈ 22 轮常驻成本）
  ② 失败不影响回话    查不到就当没查过，用原有的话回——不能因为工具坏了就不说话
  ③ 结果只进本轮      tool 消息进的是这一次请求，不写进历史；
                       写进去就会在下一轮变成"旧事实"，而模型分不清新旧
"""

from __future__ import annotations

from typing import Any, Callable

from gits.speech.llm import Reply

#: 一轮里最多调几次
MAX_CALLS = 1


def schema(catalog: tuple[dict, ...]) -> list[dict]:
    """把分类目录变成 OpenAI 兼容的 tools 定义。

    **参数一律走 JSON 字符串**：schema 里放中文描述会随语言变，放英文更稳；
    但真正要紧的是**形状固定**——同一批工具每次序列化成同样的字节。
    """
    out = []
    for t in catalog:
        name = str(t["name"])
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(t.get("about") or name),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "arg": {"type": "string",
                                "description": "可选参数（名字或 id）；不需要就留空"},
                    },
                    "required": [],
                },
            },
        })
    return out


def run(chat: Callable[..., Reply], body: list[dict], *, catalog: tuple[dict, ...],
        detail: Callable[..., dict], now: int, **kw: Any) -> Reply:
    """发起请求；模型要是想查，就查一次再让它说完。"""
    if not catalog:
        return chat(body, **kw)

    first = chat(body, tools=schema(catalog), **kw)
    calls = first.tool_calls or []
    if not calls:
        return first

    used = 0
    msgs = list(body)
    # **声明了几个 tool_call，就得有几条对应的 tool 消息。**
    # 模型偶尔一口气要查三样东西；我们只执行一个（MAX_CALLS），但协议要求
    # 每一条都有回音——少了就 400：
    #   "An assistant message with 'tool_calls' must be followed by tool messages
    #    responding to each 'tool_call_id'"
    # 这真的在线上发生过一次：那一轮整条回复丢了。
    # 做法是把超出预算的调用**从回放的 assistant 消息里去掉**，
    # 让"说过的话"和"回音"始终对得上。
    calls = calls[:MAX_CALLS]
    msgs.append({"role": "assistant", "content": first.text or "",
                 "tool_calls": calls})
    for call in calls:
        fn = (call.get("function") or {})
        name = str(fn.get("name") or "")
        try:
            args = _args(fn.get("arguments"))
        except ValueError:
            args = {}
        # **工具坏了不能让她没话说。** 查不到就当没查过——
        # 因为工具挂了就不回话，比回一句不知道糟得多。
        try:
            got = detail(name, **args)
        except Exception as exc:
            # **失败要能和"没有记录"区分开。** 都返回空对象的话，
            # 模型会以为自己查过了、只是没资料。
            got = {"error": type(exc).__name__}
        msgs.append({"role": "tool", "tool_call_id": call.get("id") or name,
                     "content": _dump(got)})
        used += 1

    second = chat(msgs, **kw)          # 不再给工具：这一轮到此为止
    second.usage = _merge(first.usage, second.usage)
    second.latency_ms = int(first.latency_ms) + int(second.latency_ms)
    return second


def _args(raw: Any) -> dict:
    import json

    if not raw:
        return {}
    obj = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(obj, dict):
        return {}
    # 只认我们声明过的那一个参数
    arg = obj.get("arg")
    if isinstance(arg, str) and arg.strip():
        return {"name": arg.strip()}
    return {}


def _dump(got: dict) -> str:
    import json

    return json.dumps(got or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _merge(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out
