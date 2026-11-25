-- G-I-T-S P0 schema v1
-- 只有 events/model_calls/observability/redactions 是"贵的"（真相），其余都是投影。

CREATE TABLE events (
  id       INTEGER PRIMARY KEY,          -- 重放游标：单调递增，唯一
  ts       INTEGER NOT NULL,             -- 节点本地钟
  recv_ts  INTEGER NOT NULL,             -- ECS 收件时间（排序权威）
  source   TEXT NOT NULL,
  kind     TEXT NOT NULL,
  ext_id   TEXT NOT NULL,                -- 确定性幂等键，不许 NULL
  node_id  TEXT,
  payload  BLOB,
  lag      INTEGER,                      -- recv_ts - ts
  ts_trust INTEGER NOT NULL DEFAULT 0,   -- 1 = ts 可用于节律训练
  UNIQUE(source, ext_id)
);
CREATE INDEX events_recv ON events(recv_ts);

CREATE TABLE model_calls (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,
  kind        TEXT NOT NULL,             -- 'extract' | 'narrative' | 'rewrite'
  model       TEXT NOT NULL,
  prompt_hash TEXT,
  request     BLOB,
  response    TEXT,
  usage_json  TEXT,
  event_id    INTEGER                    -- 它服务的那条事件
);
CREATE INDEX model_calls_event ON model_calls(event_id);

CREATE TABLE observability (
  channel  TEXT NOT NULL,
  ts_start INTEGER NOT NULL,
  ts_end   INTEGER,
  state    TEXT NOT NULL,                -- 'up' | 'down' | 'degraded'
  PRIMARY KEY(channel, ts_start)
);

CREATE TABLE redactions (
  event_id INTEGER PRIMARY KEY,
  ts       INTEGER NOT NULL
);

-- ───────────────────────── 以下全部是投影 ─────────────────────────

CREATE TABLE nodes (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  born_ts    INTEGER NOT NULL,
  scope      TEXT NOT NULL DEFAULT 'world',
  d REAL, s REAL, r REAL,
  last_touch INTEGER, centrality REAL, disclosure TEXT
);

CREATE TABLE node_content (
  id           INTEGER PRIMARY KEY,
  node_id      TEXT NOT NULL,
  content      TEXT NOT NULL,
  created_ts   INTEGER NOT NULL,
  superseded_by INTEGER
);
CREATE INDEX node_content_node ON node_content(node_id);

CREATE TABLE edges (
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  w          REAL NOT NULL,
  kind       TEXT NOT NULL,
  t_created  INTEGER NOT NULL,
  last_coact INTEGER,
  PRIMARY KEY(src, dst, kind)
);

CREATE TABLE outbox (
  id      INTEGER PRIMARY KEY,
  ts      INTEGER NOT NULL,
  channel TEXT NOT NULL,
  text    TEXT NOT NULL,
  status  TEXT NOT NULL,                 -- pending|sent|swallowed|failed|dropped|unknown
  meta    TEXT
);

CREATE TABLE influence (
  id            INTEGER PRIMARY KEY,
  outbox_id     INTEGER NOT NULL,
  node_id       TEXT NOT NULL,
  w             REAL NOT NULL,
  model_call_id INTEGER
);
CREATE INDEX influence_outbox ON influence(outbox_id);

CREATE TABLE sync_cursor (
  node_id      TEXT NOT NULL,
  stream       TEXT NOT NULL,
  last_id      INTEGER NOT NULL,
  last_sync_ok INTEGER,
  PRIMARY KEY(node_id, stream)
);
