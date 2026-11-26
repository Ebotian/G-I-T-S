-- v30: **"不知道"不等于"没命中"。**
--
-- 没有这两列的时候，provider 不返回 prompt_cache_hit_tokens 会被记成 0 命中。
-- 两轮之后 zero_streak 触发，plan() 判定"缓存过期"，换一扇新窗——
-- 于是每两轮付一次全价，而且**账单上看起来像是缓存坏了**。
--
-- 为什么是"累计"两列而不是复用 tokens/hit_tokens：
--   tokens / hit_tokens 记的是**最近一轮**（窗口长度要这个数）
--   命中率要的是**一段时间的合计**，两者口径不同，混用会让分子分母对不上
--
-- known_tokens = 命中数确实知道的轮次的 prompt token 和
-- known_hits   = 同那些轮次的命中 token 和
ALTER TABLE ctx_windows ADD COLUMN known_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ctx_windows ADD COLUMN known_hits INTEGER NOT NULL DEFAULT 0;
