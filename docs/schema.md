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

三个库，对应三种持久性（§11.8-5）：

```
gits-events.db   同步写真相。synchronous=FULL，断电最多丢最后一秒
gits-graph.db    派生。synchronous=NORMAL，丢了从 events 重放
gits-ops.db      作业与积压。丢了可以重新排队，不影响她记得什么
```

分开的理由不是洁癖：`FULL` 的 fsync 很贵，但只对 `events` 值得。

---

## 3. gits-events.db

```sql
-- 唯一真相。只 INSERT。
events(
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,   -- 事件发生时间（节点本地钟）
  recv_ts   INTEGER NOT NULL,   -- ECS 收件时间。★ 排序一律用它
  source    TEXT    NOT NULL,   -- 'wechat' | 'mic' | 'wechat_voice' | 'vision' | 'fetch' | 'self' | 'idle' | 'vital'
  kind      TEXT    NOT NULL,   -- 'msg_in' | 'msg_out' | 'acoustic' | 'image' | 'article' | 'inner' | 'node' | 'sensor'
  ext_id    TEXT,               -- 来源方 id，用于去重
  node_id   TEXT,               -- 产生它的节点（office / cloud / pocket）
  payload   BLOB,               -- CBOR。原样，不裁剪
  UNIQUE(source, ext_id)
);
CREATE INDEX events_recv ON events(recv_ts);
```

**为什么 `recv_ts` 必须存在**：多端之后两个钟会对不上（§15.6）。`ts` 是节点说"我认为当时是几点"，`recv_ts` 是"我确实在几点知道这件事"。节律模型用 `recv_ts` 学（那是可观测的），叙事里说"当时"用 `ts`。

**`payload` 用 CBOR 不用 JSON**：音频特征卡是有数组的，JSON 会把浮点数和数组编码得很胖。

**不存的东西**：原始音频（只在本地提特征，§15.6）、原始图片（摘要后删）。

---

## 4. gits-graph.db

```sql
-- 身份层：UUID 恒定，内容迭代不换身份（§12.2-1）
nodes(id TEXT PRIMARY KEY, kind TEXT, born_ts INTEGER, d REAL, s REAL, r REAL,
      last_touch INTEGER, centrality REAL, disclosure TEXT);

-- 内容层：版本链。她改主意时旧版本留在原地
node_content(id INTEGER PRIMARY KEY, node_id TEXT, content TEXT,
             created_ts INTEGER, superseded_by INTEGER);

-- 横向召回（豆辞典，§12.2-3）
aliases(node_id TEXT, keyword TEXT, PRIMARY KEY(node_id, keyword));

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
-- status: pending | sent | swallowed | failed | dropped

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
     lease_until INTEGER, attempts INTEGER, cursor TEXT, status TEXT);
-- status: queued | leased | done | failed

-- 积压（§15.7）：她的思绪有长度
backlog(kind TEXT PRIMARY KEY, n INTEGER, oldest_ts INTEGER);

-- 节点心跳
nodes_health(node_id TEXT PRIMARY KEY, last_seen INTEGER, caps TEXT, idle_since INTEGER);
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

## 8. 变更记录

| 日期 | 改了什么 |
|---|---|
| 2026-11-25 | 初版 v0 |
