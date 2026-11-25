-- v11: 回复要能追溯到触发它的那条消息。
ALTER TABLE outbox ADD COLUMN ref_event_id INTEGER;
CREATE INDEX outbox_ref ON outbox(ref_event_id);
