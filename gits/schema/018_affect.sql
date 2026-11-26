-- v18: 情绪空间 = 极坐标网格（Plutchik 圆盘：8 个角度 × 3 档强度 = 24 格）。
-- 存的是**状态**，不是日志：同一个 actor 的所有格子共享一个时间戳，
-- 读的时候用闭式解一起往前推（advance = 衰减 ∘ 径向 ∘ 环向）。
CREATE TABLE affect (
  actor     TEXT NOT NULL,
  angle     TEXT NOT NULL,
  intensity INTEGER NOT NULL,
  weight    REAL NOT NULL,
  PRIMARY KEY(actor, angle, intensity)
);
CREATE TABLE affect_state (
  actor    TEXT PRIMARY KEY,
  ts       INTEGER NOT NULL,
  unknown  REAL NOT NULL DEFAULT 0
);
