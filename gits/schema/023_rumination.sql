-- v23: 反刍闸。**没答案的问题会自我喂养。**
-- 同一件事被反复琢磨（被选为开口候选）却始终没有新证据 → 翻篇，冷却一段时间。
CREATE TABLE rumination (
  entity   TEXT PRIMARY KEY,
  strikes  INTEGER NOT NULL DEFAULT 0,
  known_at INTEGER NOT NULL DEFAULT 0,     -- 上次记 strike 时"知道几条"
  last_ts  INTEGER NOT NULL
);
