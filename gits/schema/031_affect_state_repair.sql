-- v31: **补建 affect_state。这是"改已应用的迁移"造成的洞。**
--
-- 018 最初只有 affect 一张表；五分钟后 affect_state 被加进了同一个文件。
-- 那时生产库的 user_version 已经是 18，于是那段 CREATE TABLE 永远不会再跑——
-- 文件改了，库没变。结果：relation() → affect_line() → no such table。
--
-- 教训写在 tests/test_schema_immutable.py：**已应用的迁移不许改**，
-- 新结构一律加新文件。这里用 IF NOT EXISTS，对没问题的库是空操作。
CREATE TABLE IF NOT EXISTS affect_state (
  actor    TEXT PRIMARY KEY,
  ts       INTEGER NOT NULL,
  unknown  REAL NOT NULL DEFAULT 0
);
