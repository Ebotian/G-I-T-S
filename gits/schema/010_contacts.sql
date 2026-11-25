-- v10: 联系人。回复必须带 context_token，否则挂不到会话上。
CREATE TABLE contacts (
  actor_id        TEXT PRIMARY KEY,   -- 微信里就是 xxx@im.wechat
  channel         TEXT NOT NULL,
  last_context_token TEXT,
  last_seen       INTEGER,
  last_text       TEXT
);
