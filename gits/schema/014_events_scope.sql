-- v14: 事件也分可见域。
-- 节点上有 scope，事件上却没有——而装配读的是事件。
-- 三个实例共用一份日志时，私有独白会从这里漏出去。
ALTER TABLE events ADD COLUMN scope TEXT NOT NULL DEFAULT 'world';
CREATE INDEX events_scope ON events(scope, kind);
