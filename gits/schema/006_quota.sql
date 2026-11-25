-- v6: 每个个体的每日配额
CREATE TABLE quota_day (
  day   TEXT NOT NULL,          -- 'YYYY-MM-DD'（消费方本地日，仅作账期标签）
  actor TEXT NOT NULL,
  kind  TEXT NOT NULL,
  used  REAL NOT NULL DEFAULT 0,
  lim   REAL NOT NULL,
  PRIMARY KEY(day, actor, kind)
);

-- 自主活动的元数据（**不含内容**）
CREATE TABLE activities (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  actor    TEXT NOT NULL,
  ts_start INTEGER NOT NULL,
  ts_end   INTEGER,
  kind     TEXT NOT NULL,
  cny      REAL NOT NULL DEFAULT 0,
  outputs  INTEGER NOT NULL DEFAULT 0,
  note     TEXT
);
CREATE INDEX activities_actor_ts ON activities(actor, ts_start);
