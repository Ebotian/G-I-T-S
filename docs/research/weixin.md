# 微信通道（iLink Bot）

> 状态：**通了**。收发都跑在 ECS 上，走的是 `gits-shell` 的常驻进程。
>
> 协议不是猜的：读了官方 npm 包 `@tencent-weixin/openclaw-weixin@2.4.10`
> 的 TypeScript 源码（131 个文件）照着实现的。

## 1. 协议要点

| 动作 | 端点 | 备注 |
|---|---|---|
| 取二维码 | `POST ilink/bot/get_bot_qrcode?bot_type=3` | body `{local_token_list}`，返回 `qrcode` + 一个 liteapp 链接 |
| 轮询扫码 | `GET ilink/bot/get_qrcode_status?qrcode=…` | 服务端 hold 到 35s；`confirmed` 时给 `bot_token` / `baseurl` |
| 收消息 | `POST ilink/bot/getupdates` | body `{get_updates_buf}`，返回 `msgs[]` + 新游标 |
| 发消息 | `POST ilink/bot/sendmessage` | body `{msg:{…}}` |
| 正在输入 | `POST ilink/bot/sendtyping` | 需要 `typing_ticket`（按人取）|

两个必须记住的细节：

- **`context_token`**：每条收到的消息都带，回复时原样带回，否则挂不到会话上；
- **`X-WECHAT-UIN`** = `base64(随机 uint32 的十进制字符串)`，每次请求都要变。

## 2. 实测（2026-11-25，真账号）

```
登录   bot 8c2ebb7be5c2@im.bot   ← account o9cq…@im.wechat
发送   message_id 7531361285406150664（走的是 outbox → shell → 微信）
接收   events(id=6, ext_id=7531361066903800328, payload='我又发了消息')
typing_ticket 有，长度 472
```

完整链路：

```
say-out --weixin  → outbox(pending, meta={to, context_token})
                  → shell 按 channel 名领走（weixin）
                  → send_text → status=sent, meta=<微信 message_id>
                  → outcomes(delivered)
他回一句          → shell 收 → events(msg_in) → outcomes(reply)
```

## 3. 四个只有真跑才会暴露的坑

**① `get_updates_buf` 必须落盘。**

服务端只给「新」消息。不存游标，上一轮收到的那些重启后就再也拿不回来了——
实测：拉一次拿到 1 条、没存游标，第二次同样请求返回空。所以存 `var/weixin.cursor.json`。
（官方包的 `sync-buf.ts` 也在做这件事。）

**② 同一个 token 不能开第二个 `getupdates`。**

我在 shell 之外又起了个客户端去拉，结果超时——两个长轮询抢同一个会话。
**游标归进程所有**，调试时也别旁路拉。

**③ `context_token` 只在收到消息的那一刻存在。**

第一版 shell 只存了正文，把它丢了。于是能收到、却回不了。
现在收到就写 `contacts` 表（`actor_id / last_context_token / last_seen`）。

**④ 通道要按名字领活。**

shell 同时挂文件通道和微信通道时，文件通道会把微信那一行也领走，
然后「发到文件里」——而你以为发出去了。现在每个通道声明 `names`，`claim` 按名字过滤。

## 4. 一个被验证的细节：语音条自带转写

`voice_item.text` 里已经有微信转好的文字。所以语音消息**不需要我们再跑 ASR**——
省一次调用，也省一次延时。`text_of()` 直接用它；没有转写时才退回 `[语音]`。

## 5. 还没接的

- **正在输入**：`typing_ticket` 已经能拿到（472 字符），但还没在开口前调用。
  这是吃掉 1.7 秒里最难看那段延迟的唯一手段（§2.1）。
- **图片/文件/视频**：只做了占位符，没有下载解密（AES-128-ECB + CDN）。
- **回复挂载仍是启发式**：现在挂到「还没被回过的最新一条」，因为微信侧的
  `ref_msg` 不是每条都有。

## 6. 风险

这是腾讯 2026 年正式开放的「微信 ClawBot」通道（`ilinkai.weixin.qq.com`，
有《微信ClawBot功能使用条款》）。合法路径，但它挂在**个人微信号**上。

建议：先用它做开发验证，别一上来就整天说话。等静默时段那些规矩真的生效了再放开。
