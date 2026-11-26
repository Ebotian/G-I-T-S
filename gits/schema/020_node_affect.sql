-- v20: 记忆带上自己的情感标签；同日重复开始有巩固。
-- 注意两者是**不同的东西**：
--   affect_angle/intensity  这条**消息自己**的情感强度 → 影响编码（存得多牢）
--   她此刻的分布            是另一回事 → 只影响检索（现在想起什么）
ALTER TABLE nodes ADD COLUMN affect_angle     TEXT;
ALTER TABLE nodes ADD COLUMN affect_intensity INTEGER;
-- 同日重复：第几次（用 (ts+8h)/1day 算日序，和别处时区一致）
ALTER TABLE nodes ADD COLUMN touch_day INTEGER;
ALTER TABLE nodes ADD COLUMN touch_n   INTEGER NOT NULL DEFAULT 0;
