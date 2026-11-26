-- v26: 常驻背景块的每小时快照。
--
-- 为什么必须冻结：B 段声称"一小时内逐字不变"，但 relation() 是每轮重算的，
-- 这一小时里只要落了一条新的位置/天气读数，字就变了，缓存从那里断掉。
-- **"通常不变"和"保证不变"是两件事。**
--
-- 冻结的是基线；这一小时内的实质变化另外追加（最多 3 条），追加在尾部，
-- 不动前缀。换小时整块换，一次可预期的缓存断点。
CREATE TABLE brief_snapshot (
  bucket   INTEGER PRIMARY KEY,   -- now // 3600000
  blob     TEXT NOT NULL,         -- 确定性序列化后的 JSON
  created  INTEGER NOT NULL
);
