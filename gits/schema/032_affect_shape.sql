-- v32: **把 affect 重建回正确的形状。**
--
-- 生产库里 affect 有第五列 last_ts（NOT NULL），而现在的代码只插四列：
--     IntegrityError: NOT NULL constraint failed: affect.last_ts
-- 每一条带情绪的抽取都在这里失败——**她记不住事，而 fault 里只有一行**。
--
-- 又是"改已应用的迁移"：018 最初是五列，后来改成四列，老库不会跟着改。
-- 031 只补了缺的表，没管列的差异。
--
-- 顺带说明为什么这次要重建而不是 ALTER：SQLite 不能删列（老版本），
-- 而 last_ts 是 NOT NULL 且无默认值，删不掉就只能整表重做。
CREATE TABLE affect_new (
  actor     TEXT NOT NULL,
  angle     TEXT NOT NULL,
  intensity INTEGER NOT NULL,
  weight    REAL NOT NULL,
  PRIMARY KEY(actor, angle, intensity)
);
INSERT INTO affect_new(actor, angle, intensity, weight)
  SELECT actor, angle, intensity, weight FROM affect;
DROP TABLE affect;
ALTER TABLE affect_new RENAME TO affect;
