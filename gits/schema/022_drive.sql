-- v22: 正反馈的驱动量。**不是恒温器，是有界、限速、限加速度的动量。**
-- 单独一张表：它不能用 slow_state 的均值回归（那会把正反馈变成负反馈）。
CREATE TABLE drive (
  actor TEXT PRIMARY KEY,
  value REAL NOT NULL,      -- 0..1，0.5 为中性
  rate  REAL NOT NULL,      -- 上面那个值每小时的变化率
  ts    INTEGER NOT NULL
);
