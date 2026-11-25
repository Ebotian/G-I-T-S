-- v3: 拟合好的在岗模型（系数）。dream 写，ghost 读。
CREATE TABLE presence_model (
  kind       TEXT PRIMARY KEY,      -- 'poisson_harmonic'
  fitted_ts  INTEGER NOT NULL,
  payload    TEXT NOT NULL,         -- 系数与周期项，JSON
  n_events   INTEGER NOT NULL,
  note       TEXT
);
