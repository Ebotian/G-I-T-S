# 从一条微信到她回话（2026-11-25 打通）

## 0. 链路

```
他发微信
  → shell（后台长轮询线程）→ events(kind=msg_in, ext_id=微信 message_id)
  → ghost 每拍扫一次「没有被 outbox 引用的 msg_in」
  → 装配 A/B/C → DeepSeek(thinking=off) → outbox(pending, meta={to, context_token})
  → shell 按通道名领走 → 微信 → outcomes(delivered)
他回一句 → outcomes(reply) 挂回那条 outbox
```

## 1. 实测

```
模型调用        576 ms（thinking=off，输出 2 token）
ghost 常驻内存   12.5 MB
一次调用的账     213 in / 0 hit / 57 out   ≈ 0.0002 元
```

三条真实回话（他发了三句，她各回一句）：

```
「在的」        → 「嗯，在。」
「我收到了」     → 「嗯。我看到了。」
「我又发了消息」  → 「嗯。我看见了。」
```

## 2. 一个必须显式关掉的东西：thinking

DeepSeek 默认是**思考模式**。实测 `max_tokens=40` 时：

```
usage: {completion_tokens: 40, completion_tokens_details: {reasoning_tokens: 40}}
回答: ''
```

调用成功、花掉钱、**什么都没说**。她说话不该先想 40 个字。
所以 `chat(thinking=False)` 是默认值——不是优化，是正确性。

## 3. 三个还没做对的

**① 回复太短。** 三句都是「嗯，在。」这类。人格文件写的是
「我不太会哄人。想说的东西经常短」——所以方向对，但现在的 prompt 里
C 段几乎空（没有记忆、没有关系状态），她除了「嗯」也确实没别的可说。
这不是 prompt 调优问题，是**记忆管线还没接**。

**② 缓存命中是 0。** 三次调用 `prompt_cache_hit_tokens` 全是 0。
而设计里「缓存命中价是未命中的 1/50」是成本模型的地基（§6）。
可能是前缀太短（A+B 约 150 token）、也可能是首轮不缓存。
**这条必须实测清楚**，否则 §6 的成本估算整整差一个数量级。

**③ 三句老消息在部署后被补回了。** 端到端看着是 200–900 秒，
其实是积压——她那时候还不会回话。真实延迟要等一条新消息来量。

## 4. 密钥怎么放的

```
~/gptapi.md  → 提取 deepseek key → 管道写入 /etc/gits.env（0600，root）
systemd: EnvironmentFile=/etc/gits.env
```

不进 git、不进日志、不打印。程序只从环境变量读。
