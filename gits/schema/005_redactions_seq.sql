-- v5: 删除流要有自己的单调序号
-- 原来 redactions 用被删事件的 event_id 当主键，游标按它推进 —— 删一条老事件，
-- 游标早就越过那个 id 了，增量查询永远看不到它。删除会静默丢失。
DROP TABLE IF EXISTS redactions;
CREATE TABLE redactions (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,   -- 自己的单调序号，游标用它
  event_id INTEGER NOT NULL,
  ts       INTEGER NOT NULL,
  UNIQUE(event_id)
);
CREATE INDEX redactions_ts ON redactions(ts);
