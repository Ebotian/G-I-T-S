"""出口清洗：她写出来的东西，不该带着模型的交差腔和半截话。

三条都是社区实测抓出来的，每一条背后都是"看起来正常但其实坏了"：

  **整句收笔**  max_tokens 是保险丝，不是尺。顶头就拦腰掐断——
                一条微信停在「至于信息素从哪儿来，」比不发更糟。
  **洗链接**    markdown 链接和裸网址在微信里会一整行裸奔。
  **禁清嗓子**  「我查了一下」「翻到了」这类交差话，是模型在汇报自己在干活。
                治法是**指名道姓列禁用开头**，不是软软地说"别这样"。

注意：这三条都只在**出口**做，不改提示词里的纪律——
A/B 段的风格规则是给她看的方向，这里是最后一道闸。
"""

from __future__ import annotations

import re

#: 结尾算"句子完了"的字符
ENDS = "。！？!?…～”」』\n"
#: 模型汇报自己在干活的那些开头
META_OPENERS = (
    "我查了一下", "我查了", "翻到了", "查到了", "记一下", "我去看了", "看了一眼",
    "我去查", "顺便查", "刚查", "查到", "了解到", "据报道", "根据资料",
)


def strip_links(text: str) -> str:
    """markdown 链接只留字，裸网址整个抹掉。"""
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    t = re.sub(r"\s*https?://\S+", "", t)
    return t.strip()


def strip_meta(text: str) -> str:
    """去掉"我在汇报"的开头。只剥一句，不重写内容。"""
    t = text.strip()
    for opener in META_OPENERS:
        if t.startswith(opener):
            # 剥到第一个句读之后
            for i, ch in enumerate(t):
                if ch in "，。！？：:—\n":
                    rest = t[i + 1:].lstrip("：:，,、 \n")
                    if rest:
                        return rest
                    break
            return t
    return t


def full_stop(text: str, *, cap: int = 400) -> str:
    """超帽先裁进帽内；尾巴不是句尾就退到最后一个整句；退无可退只抹掉吊尾的标点。

    宁可少说一句，也不要让一句话断在逗号上。
    """
    s = (text or "").strip()
    if len(s) > cap:
        s = s[:cap]
    if s and s[-1] not in ENDS:
        cut = max((s.rfind(c) for c in "。！？!?…"), default=-1)
        if cut >= len(s) * 2 // 5:
            s = s[:cut + 1]
        else:
            s = s.rstrip("，、；：,;: —-～")
    return s.strip()


def clean(text: str, *, cap: int = 400) -> str:
    """出口的三道闸，顺序有讲究：先剥链接（长度会变），再去腔，最后收笔。"""
    return full_stop(strip_meta(strip_links(text or "")), cap=cap)
