-- v28: 角色事实。**是记录，不是记忆**——和 contacts / facts 同一类。
--
-- 年龄由生日推导，每天自动更新，不需要写；
-- 身高体重每周日夜里由模型总结性更新，带**机械的增减量判定**。
-- 判定放在代码里而不是提示词里：模型说"长高了 8 厘米"，代码不认。
CREATE TABLE character (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,          -- 数值或日期，统一按文本存
  src        TEXT NOT NULL DEFAULT 'seed',
  updated_ts INTEGER NOT NULL
);
