"""模型调用。薄，但每一条都得算清楚。

三条约定（DESIGN §3.1 / impl §3）：

  1. **不引入 SDK**。要的是 DeepSeek 特有的字段（`safety_check`）和
     `usage` 里的缓存命中数——账单只能以它为准，不能靠假设。
  2. **每次调用都必须能被记账**。返回 `usage`，上游写进 `model_calls`。
  3. **失败要能把话说清楚**，不要抛一个 URLError 就完事。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


@dataclass
class Reply:
    text: str
    model: str
    usage: dict
    latency_ms: int


class LLMError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not k:
        raise LLMError("没有 DEEPSEEK_API_KEY（应该在 /etc/gits.env 里）")
    return k


def chat(messages: list[dict], *, model: str = DEFAULT_MODEL, max_tokens: int = 300,
         temperature: float = 1.0, safety_check: bool = False,
         thinking: bool = False, timeout: float = 60.0) -> Reply:
    """一次非流式调用。

    thinking 必须显式关掉。DeepSeek 默认是思考模式：实测 max_tokens=40 时，
    40 个 completion token 全部进了 reasoning_tokens，可见回答是空字符串——
    调用成功、花掉钱、什么都没说。她说话不该先想 40 个字。
    """
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if safety_check:
        body["safety_check"] = {"level": "high"}

    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_key()}"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMError(f"网络：{type(e).__name__}: {e}") from e

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"没有 choices：{json.dumps(payload)[:200]}")
    text = (choices[0].get("message") or {}).get("content") or ""
    return Reply(text=text.strip(), model=payload.get("model", model),
                 usage=payload.get("usage") or {},
                 latency_ms=int((time.monotonic() - t0) * 1000))


def cost_cny(usage: dict) -> float:
    """按 VERIFIED 的价目折算。**账单以 usage 为准，不以假设为准。**"""
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens")
               or max(0, int(usage.get("prompt_tokens") or 0) - hit))
    out = int(usage.get("completion_tokens") or 0)
    return hit * 1.0 / 1e6 + miss * 1.0 / 1e6 + out * 4.0 / 1e6
