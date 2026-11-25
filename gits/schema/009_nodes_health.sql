-- v9: 节点心跳。liveness 不写进 events——那是唯一真相，不能拿来放"我还活着"。
CREATE TABLE nodes_health (
  node_id    TEXT PRIMARY KEY,
  last_seen  INTEGER NOT NULL,
  caps       TEXT,
  idle_since INTEGER
);
