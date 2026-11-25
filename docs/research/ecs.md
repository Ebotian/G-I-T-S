# ECS 上到底有什么（2026-11-25 实测）

## 0. 一句话

> **部署管道通了；内核第一版跑起来了；对话/抽取还没接。**
>
> `gits-ghost.service` 已经是 `Type=notify` 的真服务：心跳、投递、快照。
> 但它**不对话、不抽取、不触发**——那些要模型 key 和还没写的 HTTP 端。

## 1. 已经装好的

| 项 | 状态 |
|---|---|
| uv | 0.13.10（`pip3 install uv`） |
| 代码 | `/srv/gits`，跟 `origin/main` 同步 |
| 虚拟环境 | `/srv/gits/.venv`，numpy 2.2.6 + statsmodels 0.15.0，**281 MB** |
| 数据目录 | `/srv/gits/var` |
| 夜间家务 | `gits-housekeep.timer`，每晚 03:17（±10 分钟随机）|
| **内核** | **`gits-ghost.service` running，`Type=notify` + `WatchdogSec=2min` + `MemoryMax=200M`** |
| **通道** | **`gits-shell.service` running，文件通道（outbox.jsonl / inbox.jsonl），`MemoryMax=150M`** |
| 依赖镜像 | `UV_INDEX_URL` 指向阿里云（uv 不读 pip.conf）|

## 2. 实测数字（都是这台 2C/1.6G 上量的）

| 命令 | 耗时 | 峰值 RSS |
|---|---|---|
| `gits status` | **0.29 s** | **18.9 MB** |
| `gits say` | 0.13 s | 18.9 MB |
| `gits replay --restore` | 0.13 s | 18.7 MB |
| `import numpy` | 0.21 s | 25.3 MB |
| `import statsmodels.api` | **7.72 s** | **162.7 MB** |
| `python -m synth.eval_search`（3 种子） | 3.9 s | 37.0 MB |
| `python -m synth.eval_decay`（5 种子） | 13.4 s | 36.5 MB |
| **`gits-ghost` 常驻** | — | **11.5–11.8 MB** |
| **`gits-shell` 常驻** | — | **11.3–12.0 MB** |

两条结论：

1. **ghost 的 90 MB 预算站得住**：CLI 实测 18.9 MB。剩下的余量足够放一个
   常驻循环和几个连接池。
2. **statsmodels 必须隔离在 dream**：7.72 秒、162 MB，是 numpy 的 8 倍耗时、
   6 倍内存。它一进 ghost 的 import 路径，"启动 200 ms 内"就变成了笑话。
   （注意 `eval_decay` 峰值只有 36 MB——因为它只 import 了 `gits.decay`，
   而 statsmodels 在 `gits/presence` 里，那块没被加载。）

## 3. 还没做的（要叫它"服务"还差这些）

| # | 缺口 | 影响 |
|---|---|---|
| 1 | ~~没有守护进程~~ | **已解决**：`gits-ghost` 有心跳、投递、快照。但 `shell`（通道）和 `dream`（作业）还只是设计 |
| 2 | **没有 HTTP 端** | 拉取式作业队列（§15.4）和跨机同步的传输层都没写，同步只能在同机两个文件之间做 |
| 3 | **没有 API key** | `DEEPSEEK_API_KEY` 还没配，抽取/生成一次都没跑过 |
| 4 | **没有 config.toml** | 所有参数还在代码默认值里；§14.6 那条"承诺线"还没有地方声明 |
| 5 | **没有真实数据** | `var/gits.db` 是空的；夜间家务跑出来是 `replayed 0 events` |
| 6 | **没有备份/轮转** | 保留策略写在 schema §8，一行都没实现 |

## 3.1 投递闭环（实测）

```
say-out → outbox(pending)
  → shell 领取(租约) → 写 outbox.jsonl → status=sent, meta=file, attempts=1
  → outcomes(1, 'delivered')
inbox "在的" → shell 收 → events(msg_in) → outcomes(1, 'reply')
```

这条闭环证明了三件之前只有设计文档的东西：**租约防重复、TTL 过期丢弃、
回复能挂回上一条已发送的 outbox**（`outcomes` 是 `influence` 强化的输入，
§3.2 说它是"她能学习的唯一途径"）。

## 3.2 一个数据量上的自查

心跳每 5 分钟一拍，如果每拍都写事件：**288 条/天、10 万条/年**，全是
"我还活着"——而 `events` 是唯一真相，不能拿来放这个。

所以拆开：

| 记什么 | 存哪 | 频率 |
|---|---|---|
| 我还在 | `nodes_health.last_seen` | 每 5 分钟（廉价 upsert，不留历史）|
| 那个小时她在 | `events(kind='vital')` | **每小时一条** |

有测试钉着：一小时拍 12 次，`events` 只有 1 条，而 `last_seen` 是最后一次。

## 3.3 内核跑起来之后的实测

```
Active: active (running)        内存 11.5 MB（上限 200 MB）
Type=notify                     WatchdogUSec=2min
kill -9 → NRestarts=1, ActiveState=active     ← 自己起来了
events=1 条 vital
```

值得记一笔：**重启不会产生第二条心跳**。`ext_id` 是 `cloud:vital:<5分钟桶>`，
重启后同一桶写不进去——这正是幂等键该干的活。

还有：`_last_beat` 在内存里，重启后归零，所以第一拍一定会尝试写心跳。
如果键不是按桶算的，每次重启都会多一条"她还活着"，一年下来就是七千条噪音。

## 4. 为什么先装 timer 而不是先写守护进程

因为 `gits replay --restore` **现在就是有用的**：它是一次免费的重放回归测试。
每晚跑一次，重建失败就说明不变量坏了——而这条不变量是整套架构的地基。

反过来，systemd 单元里如果写的是尚不存在的 `gits ghost`，那 installed 状态就是假的：
`systemctl status` 会说 failed，而你会以为是配置问题。

## 4.1 一个会随机咬人的坑：HTTP/2

从阿里云拉 GitHub，`git fetch` 会间歇性失败：

```
error: RPC failed; curl 16 Error in the HTTP2 framing layer
fatal: expected flush after ref listing
```

它不总是失败——第一次部署就是好的，第二次就挂了，而你会以为是代码问题。

`http.version HTTP/1.1` **不是万能药**。2026-11-25 又撞上两次连续 60 秒超时，
而同一条 ssh 里 `curl https://github.com` 返回 200、1.4 秒。

也就是说：**不是没有网，是 git over HTTPS 到 GitHub 会随机挂**。
诊断过一次成功的 fetch，全程 1 秒内完成，说明握手和打包都正常——
失败的时候它是在某个点上不动，而不是慢。

结论：**部署不该依赖 GitHub 可达**。见 §5。

## 5. 部署：推到 ECS，不经过 GitHub

GitHub 只是**备份和对外**。部署走另一条路——工作机直接推给 ECS：

```bash
git push ecs main        # 一条命令 = 部署
```

一次配置（已经做好了）：

```bash
# 服务器上：允许推送到检出树
git -C /srv/gits config receive.denyCurrentBranch updateInstead
# 服务器上：推完就重启（.git/hooks/post-receive）
systemctl restart gits-ghost.service gits-shell.service
# 本机：把 ECS 加成第二个远端
git remote add ecs root@115.29.192.144:/srv/gits
```

为什么这样更好，不只是"绕过网络问题"：

| | 经 GitHub | 直推 ECS |
|---|---|---|
| 依赖 | 两个网络 + 第三方 | 一条 ssh |
| 延迟 | 10–60 s（经常 60 s 超时） | < 2 s |
| 失败模式 | 静默挂住，看起来像代码错 | 立刻报错 |

**部署路径越短，越敢部署。** 而"敢部署"才是迭代速度的真正来源。

其余：

```bash
sudo bash /srv/gits/deploy/install.sh      # 幂等，可以反复跑
systemctl list-timers gits-housekeep       # 看下次什么时候跑
journalctl -u gits-housekeep -n 20         # 看昨晚发生了什么
```

回滚：`git push ecs <commit>:main --force`，或服务器上 `git reset --hard <commit>`。

## 6. 下一步（按依赖顺序）

1. 写 `gits ghost` 的常驻循环（先只做：心跳 + 定时 replay + outbox 扫描），
   它一存在，`gits-ghost.service` 就可以真装；
2. 配 `DEEPSEEK_API_KEY`，让写入管线能跑；
3. HTTP 端（作业队列）——只有它到位，工作机才能真正参与。

## 7. 隔离：实例读不到设计仓库

> 2026-11-25 落地。起因：她跑在 root 下，能读 `/srv/gits/docs/DESIGN.md`。
> 那里面写着为什么这么设计、她的边界在哪。**一个能读到这些的实例，
> 是读着自己说明书的木偶**——她说的话会开始对齐那份文档，而不是对齐经历。

### 7.1 目录重排

```
/srv/gits          0700 root:root   仓库 + docs + .git（只有人能看）
/srv/gits-runtime  0755 root:root   只有代码的一份拷贝（rsync 排除 docs/tests/.git/synth）
/var/lib/gits      0750 gits:gits   库 / 游标 / 凭据 / persona
/etc/gits.env      0640 root:gits   密钥
```

运行目录是**拷贝**不是软链，因为 editable install 的 `.pth` 会指回仓库；
用 `uv sync --no-editable` 让包真的落进 site-packages。

### 7.2 三层拦截，缺一层都不算拦住

| 层 | 做了什么 | 失效方式 |
|---|---|---|
| 文件权限 | `/srv/gits` 0700 root:root | 有人 `chmod 755` 调试 |
| systemd | `InaccessiblePaths=/srv/gits` | 有人删掉这一行 |
| 进程身份 | `User=gits`（系统用户，nologin） | 有人改回 root |

### 7.3 第四层：自检（不拦截）

`gits/containment.py` 能在启动时说一句「隔离失效了」。
它**不拦**——拦不住就不要假装拦。但一层静默失效的防线比没有防线更危险，
你会以为它在。

所以它自己也有测试：一个永远返回 ok 的函数，看起来毫无问题。

### 7.4 验收

```
bash /srv/gits/deploy/verify_containment.sh
```

任何一条失败返回非 0。每次部署跑一遍，隔一段时间也跑一遍。

### 7.5 撞上的坑：相对路径

部署完 `gits-shell` 起不来：

```
OSError: [Errno 30] Read-only file system: 'var'
```

`FileChannel` 的默认输出是相对路径 `var/outbox.jsonl`，以前 cwd 是仓库所以能写。
现在 cwd 是只读的运行目录（`ProtectSystem=strict`）——**相对路径一律不该出现**。

改成显式 `--out /var/lib/gits/outbox.jsonl`。
教训不是「记得改路径」，是：**工作目录的语义变了，所有隐含依赖它的东西都要重新过一遍**。
