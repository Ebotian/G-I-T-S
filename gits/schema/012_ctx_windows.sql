-- v12: 上下文窗口。缓存是有"代"的：换一扇窗，前缀全废。
CREATE TABLE ctx_windows (
  id           INTEGER PRIMARY KEY,
  started_ts   INTEGER NOT NULL,
  closed_ts    INTEGER,
  first_event_id INTEGER,
  last_event_id  INTEGER,
  turns        INTEGER NOT NULL DEFAULT 0,
  tokens       INTEGER NOT NULL DEFAULT 0,   -- 最近一轮的 prompt token（窗口长度）
  hit_tokens   INTEGER NOT NULL DEFAULT 0,   -- 最近一轮命中的 token
  zero_streak  INTEGER NOT NULL DEFAULT 0,   -- 连续 0 命中的次数
  close_reason TEXT
);
CREATE INDEX ctx_windows_open ON ctx_windows(closed_ts);
