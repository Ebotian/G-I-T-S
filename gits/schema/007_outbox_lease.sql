-- v7: 投递要有租约。两个 shell 同时跑时，不能把同一条发两次。
ALTER TABLE outbox ADD COLUMN lease_until INTEGER;
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX outbox_status ON outbox(status, lease_until);
