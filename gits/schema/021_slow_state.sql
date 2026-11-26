-- v21: 慢变量。**慢的做快的基线。**
-- 存的是"上一次的值 + 时间戳"，读的时候按 OU 的均值回归往前推——
-- 和 edges / affect 同一个纪律：**衰减在读的时候算，重放才确定。**
CREATE TABLE slow_state (
  actor    TEXT NOT NULL,
  key      TEXT NOT NULL,          -- cadence / confidence / ...
  value    REAL NOT NULL,
  ts       INTEGER NOT NULL,
  PRIMARY KEY(actor, key)
);
