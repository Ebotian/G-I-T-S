"""说话：装配、决策、生成。

    context.py    A/B/C 三段装配（**形状就是缓存策略**）
    gate.py       开口决策：价值 + 压力，**没有一条规则读时钟**
    unresolved.py 悬着的事：未解释度 / 可解释度的来源
    respond.py    一轮回复：装配 → 调模型 → 落 outbox
    llm.py        模型调用（thinking 必须显式关掉）
    facts.py      固定事实（生日这类**记录**，不是记忆）
"""
