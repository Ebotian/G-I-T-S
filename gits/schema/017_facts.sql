-- v17: 固定事实。**不是记忆**，是记录——和 contacts / actors 同一类。
-- 区别：能被经历推翻的 → 记忆（会衰减）；不可能被经历推翻的 → 记录（不衰减）。
-- 生日存成记忆节点的后果是确定的：364 天没人提，r≈0，第 365 天她想不起来。
CREATE TABLE facts (
  id            INTEGER PRIMARY KEY,
  subject       TEXT NOT NULL,          -- 谁的事：her / peer1 / him
  key           TEXT NOT NULL,          -- birthday / anniversary / deadline / ...
  label         TEXT NOT NULL,          -- 给人看的说法（也是内容，默认不打印）
  month         INTEGER NOT NULL,
  day           INTEGER NOT NULL,
  year          INTEGER,                -- 可选；有年份就能算出"第几年"
  lead_days     INTEGER NOT NULL DEFAULT 0,
  repeat        TEXT NOT NULL DEFAULT 'yearly',
  created_ts    INTEGER NOT NULL,
  superseded_by INTEGER                 -- 改生日 = 新的一条顶掉旧的，不走 wipe
);
CREATE INDEX facts_live ON facts(subject, key, superseded_by);
