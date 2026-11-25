# 表结构草案 v0

> **本文件不是契约，是一份正在长的草案。** 每次讨论都会改，改了就在 §9 记一行。
> 唯一的判断标准是：**改这张表的代价有多大**——见 §1。

创建：2026-11-25　｜　状态：草案，可以随便改

---

## 1. 只有一张表是贵的

| | 能不能随便改 | 为什么 |
|---|---|---|
| `events` | ❌ **贵** | 唯一真相。改字段意味着历史事件要迁移；改语义意味着过去的记忆全部失真 |
| 其余所有表 | ✅ **便宜** | 都是从 `events` 派生出来的投影。删了重建就行（§3.1 铁律） |

所以讨论顺序应该是反的：**先把 `events` 想清楚，别的先随手写，反正要重建。**

这也是为什么现在就可以落草案——只要 `events` 的骨架对，其他表改一百次都不心疼。**注意：便宜 ≠ 可以乱改**，便宜指的是"改错的代价低"，不是"不用想"。

---

## 2. 库的划分

三个库，对应三种持久性（§11.8-5）。**工作机另有一份只读副本**（§15.8）：

```
gits-events.db   同步写真相。synchronous=FULL，断电最多丢最后一秒
gits-graph.db    派生。synchronous=NORMAL，丢了从 events 重放
gits-ops.db      作业与积压。丢了可以重新排队，不影响她记得什么

工作机副本（只读，可整个删掉重建）
gits-replica.db  = 日志集副本（events / model_calls / observability / redactions）+ 本地重放出的图
gits-pending.db  = events_pending（唯一写入口，收到 ECS 提交应答后才删除）
                   ★ 与副本分开：副本可整库重建，pending 不行（§15.8.2）
```

分开的理由不是洁癖：`FULL` 的 fsync 很贵，但只对 `events` 值得。

---

## 3. gits-events.db

```sql
-- 唯一真相。只 INSERT。
events(
  id        INTEGER PRIMARY KEY,   -- ★ 重放游标用它，单调递增
  ts        INTEGER NOT NULL,      -- 事件发生时间（节点本地钟）
  recv_ts   INTEGER NOT NULL,      -- ECS 收件时间。排序一律用它
  source    TEXT    NOT NULL,      -- 'wechat' | 'mic' | 'wechat_voice' | 'vision' | 'fetch' | 'self' | 'idle' | 'vital'
  kind      TEXT    NOT NULL,      -- 'msg_in' | 'msg_out' | 'acoustic' | 'image' | 'article' | 'inner' | 'node' | 'sensor'
  ext_id    TEXT    NOT NULL,      -- ★ 确定性幂等键，不许 NULL（见 §3.1）
  node_id   TEXT,
  payload   BLOB,
  lag       INTEGER,               -- recv_ts - ts，上传延迟（§14.0）
  ts_trust  INTEGER,               -- 1 = 该事件的 ts 可用于节律训练
  UNIQUE(source, ext_id)
);
CREATE INDEX events_recv ON events(recv_ts);

-- 删除的影子表（§15.8.1）：整库重建时靠它把已删的再删一遍
redactions(event_id INTEGER PRIMARY KEY, ts INTEGER);

-- 可观测性：她当时能不能看见（§14.4）
observability(channel TEXT, ts_start INTEGER, ts_end INTEGER,
              state TEXT,        -- 'up' | 'down' | 'degraded'
              PRIMARY KEY(channel, ts_start));

-- 每一次模型调用（§3.1 恢复重放与成本核算都靠它）
model_calls(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, model TEXT,
            prompt_hash TEXT, request BLOB, response TEXT, usage_json TEXT);
```

**为什么 `ext_id` 必须是 NOT NULL**：SQLite 里 `NULL != NULL`，所以可空的幂等键等于没有幂等键，重复投递会进两条。每个来源必须能造出确定性键（见 impl §4.2），造不出的来源**不许进 `events`**。

**为什么 `recv_ts` 必须存在**：多端之后两个钟会对不上（§15.6）。`ts` 是节点说"我认为当时是几点"，`recv_ts` 是"我确实在几点知道这件事"。节律模型用 `recv_ts` 学（那才是可观测的），叙事里说"当时"用 `ts`。

**`payload` 用 CBOR 不用 JSON**：音频特征卡是有数组的，JSON 会把浮点数和数组编码得很胖。

**不存的东西**：原始音频（只在本地提特征，§15.6）、原始图片（摘要后删）。


---

## 4. gits-graph.db

```sql
-- 身份层：UUID 恒定，内容迭代不换身份（§12.2-1）
nodes(id TEXT PRIMARY KEY, kind TEXT, born_ts INTEGER, d REAL, s REAL, r REAL,
      last_touch INTEGER, centrality REAL, disclosure TEXT,
      scope TEXT DEFAULT 'world');   -- 'world' | 'self:<actor_id>'（§13.4）

-- 内容层：版本链。她改主意时旧版本留在原地
node_content(id INTEGER PRIMARY KEY, node_id TEXT, content TEXT,
             created_ts INTEGER, superseded_by INTEGER);

-- 横向召回（豆辞典，§12.2-3）
aliases(node_id TEXT, keyword TEXT, PRIMARY KEY(node_id, keyword));

-- 她的行动手艺（§18.2）：写过的脚本可以调回来
-- 用 kind='procedure' 存在 nodes/node_content 里，内容就是脚本源码

-- 赫布边
edges(src TEXT, dst TEXT, w REAL, kind TEXT, t_created INTEGER, last_coact INTEGER,
      PRIMARY KEY(src, dst, kind));

-- 社交层（§13.2）：只有 3-5 个节点，不是上万
actors(id TEXT PRIMARY KEY, kind TEXT, name TEXT, layer INTEGER, tz TEXT);
attitudes(src TEXT, dst TEXT, valence REAL, certainty REAL, ts INTEGER,
          PRIMARY KEY(src, dst));
rituals(id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER, kind TEXT,
        shared_focus REAL, sync REAL, latency_norm REAL, barrier REAL,
        outcome TEXT, d_ee REAL);

-- 出口：swallowed 是一等状态，不是"丢弃"（§5.6）
outbox(id INTEGER PRIMARY KEY, ts INTEGER, channel TEXT, text TEXT,
       status TEXT, meta TEXT);
-- status: pending | sent | swallowed | failed | dropped | unknown
-- channel: desk_audio | wechat_voice | wechat_text | mumble(可选)   ← §5.6 的阶梯
-- 'unknown' 是必需的：平台收下了但本地崩了，既不能当 sent 也不能当 failed（§17）

-- 世界怎么回应（§3.2）：她能学习的唯一途径
outcomes(id INTEGER PRIMARY KEY, outbox_id INTEGER, ts INTEGER, kind TEXT, ref_event_id INTEGER);
influence(id INTEGER PRIMARY KEY, outbox_id INTEGER, node_id TEXT, w REAL);

-- 节律（§14）
presence_kde(bin INTEGER PRIMARY KEY, w REAL, updated_ts INTEGER);  -- 168 格
changepoints(ts INTEGER PRIMARY KEY, p REAL, note TEXT);
state_snapshot(ts INTEGER PRIMARY KEY, phase TEXT, s_user REAL, s_node REAL,
               backlog INTEGER, note TEXT);
```

**未定的地方**（故意留白，见到真实数据再定）：
- `nodes.kind` 的枚举：现在设想 `fact|episode|acoustic|visual|article|self`，但很可能 `acoustic` 和 `visual` 会拆成两层（原始特征 / 语义摘要）
- `edges.kind`：`cooccur|causal|same_topic`，还没想清楚要不要 `causal`
- `node_content.content` 是纯文本还是带结构（JSON）：倾向纯文本，因为要进 prompt
- 向量放哪：`embeddings` 表还是 `node_content` 旁边一列？**先不建**，等 embedding 跑通再说

---

## 5. gits-ops.db

```sql
-- 拉取式作业队列（§15.4）
jobs(id TEXT PRIMARY KEY, kind TEXT, payload BLOB, created_ts INTEGER,
     lease_until INTEGER, attempts INTEGER, cursor TEXT, status TEXT,
     source TEXT);   -- 'dream' | 'self'（§18：她自己发起的活儿）
-- status: queued | leased | done | failed

-- 积压（§15.7）：她的思绪有长度
backlog(kind TEXT PRIMARY KEY, n INTEGER, oldest_ts INTEGER);

-- 节点心跳
nodes_health(node_id TEXT PRIMARY KEY, last_seen INTEGER, caps TEXT, idle_since INTEGER);

-- 副本同步游标（§15.8）：按流各记一个，不是只有一个 id
sync_cursor(node_id TEXT, stream TEXT, last_id INTEGER,
            last_sync_ok INTEGER, PRIMARY KEY(node_id, stream));
-- stream: events | model_calls | observability | redactions

-- 配给制（§18.4）：她是知情的，余额是她的状态而不是你的开关
quota(day TEXT, kind TEXT, used INTEGER, limit INTEGER, PRIMARY KEY(day, kind));
-- kind: llm_in | llm_out | fetch | search | ocr | tts_chars | asr_sec | exec_sec
```

---

## 6. 迁移纪律

不用 ORM，不写 automigrate。三条规矩：

1. **每次改表 → 一条 `migrations/NNN_name.sql`**，配 `PRAGMA user_version` 递增。
2. **派生库永远可以整库删除重建**（`gits rebuild-graph`）。这条是安全网：如果某个迁移写坏了，丢掉 graph 库重放就行。
3. **`events` 只做加法**：加列、加索引、加枚举值。不改语义、不改已有列的存储格式。

---

## 7. 还没解决的两个问题

1. **`outbox` 的 `swallowed` 要不要回流成 `self` 事件？**
   现在的想法是要（§10.3 说这是全系统最好的闭环），但那样 `outbox` 和 `events` 会有一次写放大。倾向：`swallowed` 直接写一条 `events(source='self', kind='inner')`，`outbox` 里只留指针。
2. **`state_snapshot` 每 5 分钟一拍，一年 10 万行。**
   太小不用管，但要不要压缩成"每小时精确、每天聚合"？**先不管**，等到它变成问题它才会变成问题。

---

## 8. 保留与删除（接入真实数据前必须定）

审计提出的这条不是洁癖，是**这个系统能不能长期存在的前提**：一个只增不删的日志，最终会变成一份关于你生活的、不可撤回的档案。

### 8.1 分级保留

| 数据 | 保留 | 理由 |
|---|---|---|
| 原始音频 | **0**（不上传，本地 7 天轮转） | 办公室常开麦不该有云端副本 |
| 原始图片 | 摘要后即删 | 同上 |
| `events.payload`（消息正文） | 180 天，之后只留 `kind` + 摘要节点 | 长期价值在"她记得"，不在"原文还在" |
| `model_calls.request` | 30 天 | 复盘用，太久没用 |
| `model_calls.response` | 永久（可裁剪成 diff） | **恢复重放的必要条件** |
| `nodes` / `node_content` | 永久 | 这就是她 |

注意一句：**"遗忘衰减"不删数据，只改召回权重**（§5.1.2）。这两件事必须分开——把"忘记"实现成"删掉"是不可逆的，而 §5.5 的自我叙事恰恰需要能回看旧版本。

### 8.2 删除语义

```
gits forget --event <id>      只删这一条 payload，保留行（kind/时间不动）
gits forget --node <id>       删节点 + 内容链 + 别名 + 边，并写下一条 tombstone
gits forget --actor <id>      整个实例的所有 self:* 记忆
```

三条硬要求：

1. **删除必须写 tombstone 到 `events`**（`kind='redact'`）。否则下次恢复重放会把删掉的东西又造回来——这是备份/重放系统里最经典的坑。
2. **删除要覆盖备份**。备份文件按周轮转，最长 30 天；一次 `forget` 之后，30 天内的备份里仍有旧数据。**这个窗口必须写在文档里，而不是让人以为删了就没了。**
3. **发给第三方的部分收不回来**。DeepSeek 的请求一旦发出，就不在我们的控制范围内。所以 §5.7 的透明性声明里要有一句实话。

### 8.3 这一节的地位

它是**接入真实微信数据之前的门槛**，不是"以后再说"。理由很朴素：**先有删除能力，才有资格记录。**

---

## 9. 时间戳的三个字段

| 字段 | 权威性 | 用途 |
|---|---|---|
| `ts` | 节点本地钟，**仅供参考** | 叙事里说"当时" |
| `recv_ts` | **ECS，唯一权威**（NTP，Asia/Shanghai） | 排序、学习、窗口、游标之外的一切 |
| `label` | 由 `recv_ts` 派生（小时-周、相位） | **只喂算法当坐标，不写进 prompt** |

原则见 DESIGN §14.0：**墙钟做参照和标签，不参与行为决策。** 所以任何"9 点""凌晨""每日"都不应该出现在决策代码里，只应该出现在 `label` 的生成函数里，以及一条声明式的承诺（§14.6）。

## 10. 变更记录

| 日期 | 改了什么 |
|---|---|
| 2026-11-25 | 初版 v0 |
| 2026-11-25 | 加 §9 时间戳三个字段；副本同步游标 `sync_cursor`；`events_pending` |
| 2026-11-25 | 审计后修：`ext_id` 改 NOT NULL（可空等于没有幂等）；新增 `observability`、`model_calls`；`nodes.scope`；`outbox.status` 增加 `unknown`；新增 §8 保留与删除 |
