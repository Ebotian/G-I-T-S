-- v27: 她可以给地方起名字。
--
-- 名字是**标注**，不是学出来的：从"夜里 90% 在这"推断出"这是家"是猜。
-- 但标注也得可追溯——所以走事件，重放能重建，wipe 能清掉。
CREATE TABLE place_alias (
  place_id  INTEGER NOT NULL,
  alias     TEXT NOT NULL,
  src       TEXT NOT NULL DEFAULT 'her',   -- her / user / seed
  ts        INTEGER NOT NULL,
  PRIMARY KEY(place_id, alias)
);
