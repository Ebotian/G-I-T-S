-- v13: 每条入站消息自带路由。回复的收件人由**触发它的那条消息**决定，
-- 不是"最近联系人"。只有一个用户时看不出区别，但这种 bug 只会在
-- 有第二个人的那一天才现形，而那时已经发错了。
CREATE TABLE event_route (
  event_id       INTEGER PRIMARY KEY,
  channel        TEXT NOT NULL,
  actor_id       TEXT NOT NULL,
  context_token  TEXT
);
