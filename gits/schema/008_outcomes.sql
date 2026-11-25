-- v8: 世界怎么回应。§3.2 说这是"她能学习的唯一途径"，但 P0 建表时漏了。
CREATE TABLE outcomes (
  id           INTEGER PRIMARY KEY,
  outbox_id    INTEGER NOT NULL,
  ts           INTEGER NOT NULL,
  kind         TEXT NOT NULL,      -- 'delivered' | 'reply' | 'ignored' | 'reaction'
  ref_event_id INTEGER
);
CREATE INDEX outcomes_outbox ON outcomes(outbox_id);
