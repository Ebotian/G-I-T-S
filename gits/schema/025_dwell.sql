-- v25: 采样不是均匀的，所以"一条定位"不等于"一个时刻"。
--
-- 手机上报是**位置变了才触发**，不是定时轮询。于是：
--   他在家待一整天   -> 一条定位代表 8 小时
--   他通勤来回走     -> 一条定位代表 2 分钟
--
-- 按"每条定位"算率，等于在移动时段过采样、在久坐时段欠采样。
-- 而且久坐的地方恰恰是"家"——**最重要的地方可能定位最少**。
--
-- 所以每条定位要带上它代表的那段时长，率按时间加权，不按条数。
ALTER TABLE place_fixes ADD COLUMN dwell_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE places ADD COLUMN run_dwell_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE places ADD COLUMN best_dwell_ms INTEGER NOT NULL DEFAULT 0;
