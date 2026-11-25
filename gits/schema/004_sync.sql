-- v4: 分层同步 —— 产物身份、职责租约
ALTER TABLE model_calls  ADD COLUMN producer TEXT;
ALTER TABLE nodes        ADD COLUMN producer TEXT;
ALTER TABLE node_content ADD COLUMN producer TEXT;

-- 大产物的元数据（向量本身不传，只传"我是用什么产的"）
CREATE TABLE artifact_meta (
  kind     TEXT NOT NULL,      -- 'embedding' | 'acoustic'
  key      TEXT NOT NULL,      -- node id 或片段 hash
  producer TEXT NOT NULL,      -- 'office/bge-m3@v1'
  dim      INTEGER,
  ts       INTEGER NOT NULL,
  PRIMARY KEY(kind, key)
);

-- 职责租约：同一时刻只有一个节点能写某类产物，避免两端各写一份人格
CREATE TABLE roles (
  role        TEXT PRIMARY KEY,   -- 'self_narrative' | 'presence_model' | 'attitudes'
  holder      TEXT NOT NULL,
  lease_until INTEGER NOT NULL,
  epoch       INTEGER NOT NULL DEFAULT 0
);
