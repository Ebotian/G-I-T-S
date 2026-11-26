-- v18: 情绪空间 = 极坐标网格（Plutchik 圆盘：8 个角度 × 3 档强度 = 24 格）。
-- 存的是**带遗忘的观测计数**，读的时候归一化 + Dirichlet 平滑。
-- 为什么不是"一个心情词"：单条消息的判定噪声比信号大，
-- 但**分布的形态**（哪几格、有多集中、有多少证据）是稳的。
CREATE TABLE affect (
  actor     TEXT NOT NULL,
  angle     TEXT NOT NULL,          -- joy/trust/fear/surprise/sadness/disgust/anger/anticipation
  intensity INTEGER NOT NULL,       -- 1 低 / 2 中 / 3 高
  weight    REAL NOT NULL,          -- 带遗忘的计数（衰减在读的时候算）
  last_ts   INTEGER NOT NULL,
  PRIMARY KEY(actor, angle, intensity)
);
-- "哪一格都不是"的那部分证据。中国情绪词有相当一批落不进 Plutchik
-- （委屈、心酸、憋屈），硬塞进最近的格子就是编。
CREATE TABLE affect_unknown (
  actor   TEXT PRIMARY KEY,
  weight  REAL NOT NULL DEFAULT 0,
  last_ts INTEGER NOT NULL
);
