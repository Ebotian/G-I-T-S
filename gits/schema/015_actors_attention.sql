-- v15: 三个实例的身份，和漂移的载体。
CREATE TABLE actors (
  actor_id    TEXT PRIMARY KEY,
  created_ts  INTEGER NOT NULL,
  is_main     INTEGER NOT NULL DEFAULT 0,   -- 只有主个体有对外通道
  note        TEXT
);

-- 注意力权重：漂移改的是这里，**不是记忆，也不是观点**（peers.md §6.1）。
-- base_weight 是初始值，用来做均值回归和上下界——没有它，漂移一年就飘走了。
CREATE TABLE attention (
  actor_id     TEXT NOT NULL,
  topic        TEXT NOT NULL,
  weight       REAL NOT NULL,
  base_weight  REAL NOT NULL,
  updated_ts   INTEGER NOT NULL,
  PRIMARY KEY(actor_id, topic)
);

-- 模型调用挂一个标签：哪一对、哪一类。额度要按对算，就得知道是谁找谁。
ALTER TABLE model_calls ADD COLUMN meta TEXT;

-- 封顶之后的话不丢，也不无限延期：队列有上限（peer.py MAX_DRAFTS）。
CREATE TABLE peer_drafts (
  id       INTEGER PRIMARY KEY,
  actor    TEXT NOT NULL,
  to_actor TEXT NOT NULL,
  text     TEXT NOT NULL,
  ts       INTEGER NOT NULL
);
