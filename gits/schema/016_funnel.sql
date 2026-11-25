-- v16: 漏斗。压缩是有损且不可逆的——但**不可检索 ≠ 不可审计**。
-- 子内容被吸收后进不了检索，仍然查得到它是从哪条抽象出来的。
ALTER TABLE node_content ADD COLUMN absorbed_into INTEGER;

-- 深度：0 = 原始经历，越大越抽象。人格就是漏到最底的那几条。
ALTER TABLE nodes ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;
CREATE INDEX nodes_depth ON nodes(depth);
