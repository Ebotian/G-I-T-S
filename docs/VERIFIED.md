# 外部依赖事实（VERIFIED）

> 这份文件只放**可验证的外部事实**，不放设计判断。设计文档里每一个"平台能/不能"的断言都应能在这里找到出处。
> 新增或复核时：更新「验证日期」并保留旧结论，用删除线标记已失效的项——diff 本身就是历史。

最近一次整体复核：**2026-11-25**

---

## 1. DeepSeek 模型与价格

| 项 | 事实 | 出处 | 验证日期 |
|---|---|---|---|
| 模型 | `deepseek-flash`（DeepSeek-V4.1-Flash-1017）、`deepseek-v4-pro` | [模型 & 价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing) | 2026-11-25 |
| 上下文 | 1M；输出最大 384K | 同上 | 2026-11-25 |
| 图像理解 | **flash 支持**，v4-pro **不支持** | [vision](https://api-docs.deepseek.com/zh-cn/guides/vision) | 2026-11-25 |
| 单图 token 数 | 官方文档未给出固定值，**待实测** | — | — |
| 其他能力 | Tool Calls / JSON Output / Responses API / Anthropic 格式 / FIM(beta) / 对话前缀续写(beta) | pricing 页 | 2026-11-25 |

### 价格（元 / 百万 tokens）

| | flash 空闲 | flash 高峰 | v4-pro 空闲 | v4-pro 高峰 |
|---|---|---|---|---|
| 输入·缓存命中 | 0.02 | 0.04 | 0.15 | 0.30 |
| 输入·未命中 | 1 | 2 | 4.5 | 9.0 |
| 输出 | 4 | 8 | 13.5 | 27.0 |

- **高峰时段**：北京时间周一至周五（不含法定节假日）9:00–12:00、14:00–18:00；其余时间（含周末、节假日全天）为**空闲，半价**。
- **缓存命中价 = 未命中的 1/50**。这是本系统最重要的一条成本事实。

## 2. DeepSeek 应用类 API（2026-11-13 发布"全家桶"）

| 能力 | 端点 | 关键限制 | 价格 | 验证 |
|---|---|---|---|---|
| TTS 整段 | `POST /v1/tools/tts` | 单次 ≤10,000 计费字符；wav/pcm；音色仅 `stella`(女) / `tide`(男)；**不支持调速** | 3 元/万字符（限时 1.5） | 2026-11-25 |
| TTS 流式 | `wss://api.deepseek.com/v1/tools/tts` | 可分片喂文本；**format=opus 返回裸 Opus 包**（无容器） | 同上 | 2026-11-25 |
| ASR | `POST /v1/tools/asr` | **非流式**；单次 ≤120 秒、≤8 MiB；WAV(16bit PCM) 或 Ogg Opus；双声道只认左声道 | 0.78 元/小时（限时 0.39） | 2026-11-25 |
| OCR | `POST /v1/tools/ocr` | ≤1000 页 / 100 MiB；`purpose`=text_recognition / rich_layout_recognition；返回带 `<\|box\|>` 坐标 | 1.5 元/千页 | 2026-11-25 |
| Search | `POST /v1/tools/search` | 暂不支持图片搜索 | 5 元/千次 | 2026-11-25 |
| Fetch | `POST /v1/tools/fetch` | 单页 → Markdown | 5 元/千次 | 2026-11-25 |
| 内容安全 | 请求参数 `safety_check: {"level":"high"}` | 审输入**和**输出；覆盖 Messages / Responses / Chat Completions | 无单独定价 | 2026-11-25 |

- 2026-11-18 12:00 起，模型调用中**服务端触发的**搜索/抓取工具不再单独计费，结果按 token 计费。

## 3. 关键缺口（决定了架构）

| 缺口 | 事实 | 影响 |
|---|---|---|
| **无 embedding 接口** | DeepSeek 不提供 `/embeddings`；多方独立印证 | 语义召回必须外挂（本地 bge-m3 / 第三方 embedding） |
| **无实时双工音频** | ASR 非流式且 ≤120s；TTS 单向流式 | "全双工语音对话"不可实现；语音只能是**独白通道**或轮次较慢的对话 |
| **TTS 不能调速** | 文档明示 | 气声/低语效果只能靠**文本层面**（短句、语气词）与客户端播放增益，不能在 TTS 侧调 |

## 4. 微信通道

| 项 | 事实 | 出处 | 验证 |
|---|---|---|---|
| 官方个人号 Bot | 腾讯通过 OpenClaw 开放"微信 ClawBot"，底层协议 iLink（智联），域名 `ilinkai.weixin.qq.com` | 社区逆向文档 | 2026-11-25 |
| npm 包真实性 | `@tencent-weixin/openclaw-weixin` 存在于 npm，maintainer 为 @tencent.com 邮箱，latest = **2.4.10**，周下载 ~1.8 万 | `registry.npmjs.org` 直查 | 2026-11-25 |
| 收消息 | `POST /ilink/bot/getupdates` 长轮询，服务端 hold ≤35s，用 `get_updates_buf` 作游标 | 同上 | 2026-11-25 |
| 发消息 | `POST /ilink/bot/sendmessage`，**必须原样回带 `context_token`** 才能挂到正确会话 | 同上 | 2026-11-25 |
| 消息类型 | 1 文本 / 2 图片(CDN 加密) / 3 语音(**silk 编码，自带转写文字**) / 4 文件 / 5 视频 | 同上 | 2026-11-25 |
| 正在输入 | `sendtyping` + `getconfig` 取 typing_ticket | 同上 | 2026-11-25 |
| ⚠️ 版本耦合 | npm dist-tag 出现 `compat-host-gte2026.3.0-lt2026.3.22`，说明通道包与 OpenClaw host 版本强绑定 | 同上 | 2026-11-25 |

## 5. OpenClaw（若用作通道宿主）

| 项 | 事实 | 验证 |
|---|---|---|
| 自定义 provider | `models.providers.<id>.api = "openai-completions"` + `baseUrl` + `apiKey` 即可接任意 OpenAI 兼容后端 | 2026-11-25 |
| 插件类型 | provider / tool / hook / **channel** 插件；channel 插件实现 `ChannelPlugin` 接口 | 2026-11-25 |
| 结论 | 我们的内核可以作为"一个 provider"接进去，也可以写成"一个 channel"把消息透传出来 | — |

## 6. 算法部分

| 项 | 事实 | 验证 |
|---|---|---|
| FSRS-5 | **19 个可训练参数**（w0..w18），ts-fsrs 6.x 强制校验长度 19 | 2026-11-25 |
| FSRS-5 默认权重 | `[0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.0234, 1.616, 0.1544, 1.0824, 1.9813, 0.0953, 0.2975, 2.2042, 0.2407, 2.9466, 0.5034, 0.6567]` | 2026-11-25 |
| 幂律遗忘 | `R(t,S) = (1 + FACTOR·t/S)^DECAY`，FSRS 取 FACTOR=19/81, DECAY=-0.5 | 2026-11-25 |
| 稀疏图传播 | `scipy.sparse` 上 k-hop 迭代 `e ← (1-γ)e + κ·A_norm·e + u`，单次 O(E) | — |

## 7. 待验证（不阻塞设计，但影响参数）

- [ ] 单张图片实际消耗 token 数（影响 Vision 预算）
- [ ] `/v1/tools/tts` 的 opus 帧序列能否直接喂 Mumble/Murmur（帧长、采样率、PT 时长）
- [ ] iLink `sendmessage` 的频率限制与单条长度上限
- [ ] 个人微信账号走 iLink 是否有"不得用于长期无人值守自动对话"之类的条款（读《微信ClawBot功能使用条款》原文）
- [ ] bge-m3 在目标 ECS 规格（几核几 G）上的单句延迟
- [ ] `safety_check` 误杀率：对亲密/玩笑语境的误判是否可接受

---

## 8. ECS 环境实测（2026-11-25）

| 项 | 值 |
|---|---|
| OS / 内核 | Ubuntu 22.04.5 / 5.15.0-186-generic |
| CPU / 内存 / 磁盘 | 2 vCPU Xeon Platinum / 1.6 GiB / 40 G（用 6.5 G） |
| swap | 2 G（`/swapfile`）——原先**没有** |
| Python | 3.10.12；`venv` 可用，pip 22.0.2 |
| SQLite | 3.37.2，**FTS5 / FTS4 / JSON1 全部可用**；WAL 与 `synchronous=FULL` 实测生效 |
| ffmpeg | 4.4.2，含 `libopus` / `pcm_s16le` / `aac` |
| 到 api.deepseek.com | TCP 10 ms，TLS 95 ms，TTFB 174–183 ms |
| 到 dashscope | TLS 134 ms |
| 对外端口 | 只剩 22（mumble 的 64738 已停） |

**`vm.swappiness` 有三个主人**：`/etc/sysctl.d/99-apsara-sysctl.conf` 写 0、tuned 的 `virtual-guest` profile 写 30、我们的 `sysctl.d` 写 10。实测开机后是 **0**——**tuned 并没有赢**，尽管它 `After=systemd-sysctl.service`。

依次把三者打回原值再重启验证：

```
after systemd-sysctl: 0
after tuned:          0
after gits-sysctl:   10
```

已改用 `gits-sysctl.service`（`After=systemd-sysctl.service tuned.service`）作为明确排在最后的写入者，钉在 10。教训：**配置的所有权必须唯一且可验证**，这以后同样适用于 `config.toml` 里的每一个魔法数字。

