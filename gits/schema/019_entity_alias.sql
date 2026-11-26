-- v19: 语义解析的别名表。
-- 实体 id 仍然是**精确字符串**（不猜）；别名是另一层，查图时先展开。
-- 来源两种：'model'（夜里压缩时顺带问）和 'cooccurrence'（共现推断，只提议不合）。
CREATE TABLE entity_alias (
  alias_id      TEXT PRIMARY KEY,
  canonical     TEXT NOT NULL,
  source        TEXT NOT NULL,
  certainty     REAL NOT NULL,
  created_ts    INTEGER NOT NULL,
  superseded_by INTEGER
);
CREATE INDEX entity_alias_canonical ON entity_alias(canonical, superseded_by);
