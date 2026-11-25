"""对话语料：模板生成 + 可选的真实语料。

真实语料是"借鉴"，不是"复制"：`load_corpus` 只读本地文件，
仓库里不放任何第三方数据。支持的格式是每行一个 JSON：
    {"user": "...", "assistant": "..."}
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Iterator, Sequence

TOPICS = (
    "开会开到想吐", "Rust 借用检查又卡住了", "昨晚三点才睡",
    "楼下咖啡换了豆子", "显示器太亮", "周末想去骑车",
    "那个 PR 又被打回", "今天没吃早饭", "耳机左耳没声了",
    "想到一个奇怪的架构", "CSS 又崩了", "想换个键盘",
)

THOUGHTS = (
    "他今天话比平时少。",
    "那句「随便」不像随便。",
    "我好像记错了他说过的时间。",
    "这个念头先放着，明天再看。",
)

REPLIES = (
    "嗯。", "又没吃？", "那就别改了，睡吧。", "我记着了。",
    "你上次也这么说。", "说具体点。", "行。", "几点？",
)


def synthetic_messages(rng: random.Random, n: int) -> list[str]:
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.6:
            out.append(rng.choice(TOPICS))
        elif r < 0.85:
            out.append(rng.choice(REPLIES))
        else:
            out.append(rng.choice(TOPICS) + "，" + rng.choice(THOUGHTS))
    return out


def load_corpus(path: str | Path) -> Iterator[tuple[str, str]]:
    """读本地 JSONL 语料。格式不对就跳过那一行，不猜。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            u, a = obj.get("user"), obj.get("assistant")
            if isinstance(u, str) and isinstance(a, str):
                yield u, a


#: 常见的开源中文多轮对话语料。**不随仓库分发**，需要自己下载后指向它。
#:   LCCC (清华)        https://github.com/thu-coai/CDial-GPT          Apache-2.0
#:   豆瓣多轮            https://github.com/MarkWuNLP/MultiTurnResponseSelection  研究用途
#:   NaturalConv         https://github.com/elliott-king/NaturalConv   研究用途
KNOWN_CORPORA = (
    ("LCCC", "https://github.com/thu-coai/CDial-GPT", "Apache-2.0"),
    ("豆瓣多轮", "https://github.com/MarkWuNLP/MultiTurnResponseSelection", "研究用途"),
    ("NaturalConv", "https://github.com/elliott-king/NaturalConv", "研究用途"),
)


def fill_messages(sc: "object", rng: random.Random, *, corpus: Sequence[tuple[str, str]] | None = None) -> None:
    """把 schedule 生成的空消息填上文本。"""
    msgs = getattr(sc, "messages")
    if corpus:
        pool = [u for u, _ in corpus]
        for i in range(len(msgs)):
            t, _ = msgs[i]
            msgs[i] = (t, pool[i % len(pool)])
        return
    texts = synthetic_messages(rng, len(msgs))
    for i, (t, _) in enumerate(msgs):
        msgs[i] = (t, texts[i])
