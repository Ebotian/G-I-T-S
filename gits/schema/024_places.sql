-- v24: 场所。**从轨迹里学出来的，不是他填的。**
-- 成员资格永久（和边的成员资格同一条纪律）：一个坐标属于过某个地方，就一直是。
-- 名字只能来自外部输入——从"20:00-08:00 有 90% 时间在这里"推断出"这是家"是猜，
-- 猜错她会在你上班时问"到家了？"。
CREATE TABLE places (
  id         INTEGER PRIMARY KEY,
  scope      TEXT NOT NULL DEFAULT 'self:her',
  lat        REAL NOT NULL,              -- 中心（按 1/acc^2 加权的运行均值）
  lon        REAL NOT NULL,
  radius     REAL NOT NULL DEFAULT 60,
  n          INTEGER NOT NULL DEFAULT 0,
  w          REAL NOT NULL DEFAULT 0,    -- 权重和
  first_ts   INTEGER NOT NULL,
  last_ts    INTEGER NOT NULL,
  run        INTEGER NOT NULL DEFAULT 0, -- 当前这一趟连续待了几次
  best_run   INTEGER NOT NULL DEFAULT 0, -- 最长的一趟
  name       TEXT                        -- 空 = 只是个"常去的地方"
);
CREATE TABLE place_fixes (
  place_id  INTEGER NOT NULL,
  event_id  INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  PRIMARY KEY(place_id, event_id)
);
-- 去抖之后的"他现在在哪"。**观测和确认分开记。**
CREATE TABLE place_state (
  actor      TEXT PRIMARY KEY,
  place_id   INTEGER,                    -- 已确认
  since      INTEGER NOT NULL DEFAULT 0,
  pending    INTEGER,                    -- 候选
  pending_n  INTEGER NOT NULL DEFAULT 0
);
-- 学出来的东西的增量游标。重建 = 清表 + 从 0 重放。
CREATE TABLE learn_cursor (
  name          TEXT PRIMARY KEY,
  last_event_id INTEGER NOT NULL DEFAULT 0
);
