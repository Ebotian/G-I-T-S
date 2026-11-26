# 空间源：我们接收，不追踪

> 时间源是本地时钟，空间源必须从外面来。这个不对称决定了整套设计。
>
> 手机 -> 服务器，单向。不是偏好，是网络拓扑。

## 1. 实测：手机没有公网 IP

    $ adb shell dumpsys connectivity | grep LinkAddresses
      rmnet_data5  LinkAddresses: [10.159.144.128/24, 2408:8440:3200:21f:.../64]
      DNS: 221.12.1.227  （浙江联通）

IPv4 是 **10.159.144.128** —— 私有地址。中国移动/联通/电信的蜂窝网络全走 CGNAT，
几千台设备共用出口。所以：

    服务器 -> 手机    不可能。没有可达地址
    手机 -> 服务器    唯一方向

「轮询后台访问 GPS」这个轮询**必须跑在手机上**，服务端只负责收。

（有 IPv6，但前缀随基站变化，且 Android 默认无入站监听——实用上仍要手机主动。）

## 2. 实测：IP 定位给的是机房，不是他

    开发机   /v3/ip -> 浙江省杭州市 330100
    ECS      /v3/ip -> 浙江省杭州市 330100
    手机 GPS 27.862,120.816 -> 浙江省温州市龙湾区 330303

两台机器同一个答案，因为解析的是机房出口 IP。**人在温州，机房在杭州。**

同类问题：高德 /v3/ip **忽略 ip 参数**（传 ip=1.2.4.8 返回空数组），
只能定位调用方自己。所以服务端没法"查他的 IP"，必须手机自己发请求。

## 3. 精度：比预期好，但自报值偏乐观

| 来源 | hAcc | 备注 |
|---|---|---|
| gps | **15.0 m** | 19 颗卫星，meanCn0=28；室内取不到 |
| fused | **30.0 m** | 小米 fused provider，bundle 里带 amap=1 |
| network | **30.0 m** | 同上，自报值通常偏乐观 |
| ip | 城市级 | 实测过 500 km 级的错（114DNS -> 济南，实际南京） |

30 m 意味着**场所聚类半径可以用 50 m**——家和公司分得开。如果只有 500 m，
它们会合并成一个点，然后她以为他一整天没动过。

分层规则：

    城市级（ip 够）              -> 天气、时区
    场所级（要 gps/wifi/network） -> 在家率、通勤率、s(t|p)

PLACE_SOURCES 就是为了钉住这条：**ip 永远不进场所层**。

## 4. 四个只有真装才暴露的坑

### 4.1 Termux:API 的签名必须和 Termux 一致

    INSTALL_FAILED_SHARED_USER_INCOMPATIBLE:
      Package com.termux.api has no signatures that match those in shared user com.termux

F-Droid 的 termux-api 装不进小米商店版 Termux。看来源：

    $ adb shell dumpsys package com.termux | grep -E "installerPackageName|pkgFlags"
      installerPackageName=com.miui.packageinstaller
      pkgFlags=[ DEBUGGABLE ... ]

DEBUGGABLE 说明是 **github-debug** 变体，它和 termux-api+github.debug、
termux-boot+github.debug 共用同一把 debug 签名。换这三个包就装上了。

（顺带：DEBUGGABLE 还让 run-as com.termux 可用，可以读写它的 home——排查问题时很值钱。）

### 4.2 run-as 里没有网络

想绕过 UI、直接用 adb shell run-as com.termux pkg install termux-api：

    [*] (1) https://mirrors.aliyun.com/termux/...: bad      <- 全部失败
    [*] (1) https://mirror.iscas.ac.cn/termux/...: bad
    Error: None of the mirrors are accessible

run-as 拿到的进程不在 app 的 SELinux 域里，网络被挡。但用 input text
打进真正的 Termux 终端（app 自己的进程）就全部 ok。

**结论：安装类操作必须在 Termux 终端里跑，不能走 run-as。**

### 4.3 Termux:Boot 里没有 HOME

第一版开机脚本：

    nohup "$HOME/gits-loc-loop.sh" >> "$HOME/gits-loc.log" 2>&1 &

开机后日志文件根本没被创建。开机上下文的 HOME 是空的，
$HOME/gits-loc-loop.sh 变成了 /gits-loc-loop.sh，静默失败。

现在一律写绝对路径：

    H=/data/data/com.termux/files/home
    export HOME="$H"
    export PATH="/data/data/com.termux/files/usr/bin:/system/bin"

### 4.4 幽灵进程杀手会杀掉子进程

电池白名单防的是"整个应用被清"，**防不了子进程被杀**——而 while :; sleep 900
正是子进程。

    $ adb shell settings get global settings_enable_monitor_phantom_procs
      null            <- 默认开启
    $ adb shell settings put global settings_enable_monitor_phantom_procs false
      false

两个都要设：白名单 + 这个开关。

## 5. 上报的形状：一次连接，不是长连接

移动中的数据连接会断：换基站、进出隧道、CGNAT 重新分配。长 SSH 会话必然被切断，
而且断了以后中间那段读数就丢了。

    连上 -> 发一条 -> 断开      每次约 5 KB

重连要退避：没信号时每 15 秒撞一次，一小时 240 次握手——白烧流量还更耗电。
现在是失败后额外睡 300 秒。

成本：每 15 分钟一条 ≈ 每天 500 KB，可以忽略。

## 6. 采集节奏：不是"轮询"，是分级

    每 15 分钟    network 定位（省电，30 m 够用）
    每 1 小时     gps（更准，室内取不到会自动退回 network）

失败不清零、不重试风暴。

## 7. 权限：Android 11+ 的硬规定

    ACCESS_FINE_LOCATION          granted
    ACCESS_COARSE_LOCATION        granted
    ACCESS_BACKGROUND_LOCATION    granted   <- 必须"始终允许"，不是"仅在使用时"

pm grant 可以直接授，不需要点 UI。但 MIUI 的**自启动**开关在系统设置里，
adb 改不了（appops set ... AUTO_START 报 Unknown operation string）。

## 8. 端点：不新开端口

    开 HTTP    要 nginx + 证书 + token + 限流     新攻击面
    走 SSH     复用已经暴露的通道，一条受限命令    零新增

    /usr/local/bin/gits-loc-apply          读 /etc/gits.env，以 her 身份跑 gits loc
    /etc/sudoers.d/gits-loc                root 可以无密码切到 her 跑这一条
    /root/.ssh/authorized_keys            command="sudo -n -u her /usr/local/bin/gits-loc-apply",restrict

实测：拿着这把钥匙执行 ssh ... id，出来的还是 gits loc 的输出——
command= 覆盖了请求的命令。

## 9. 四条数据纪律

| 纪律 | 为什么 | 测试 |
|---|---|---|
| 幂等键是**手机给的定位时间** | 重发不该在轨迹上多一个点 | test_重复上报不产生重复轨迹 |
| scope=self:her | peer 不该知道他人在哪；reading 默认是 world | test_位置是私有的 |
| 只出城市名，不出坐标 | 每轮上下文都发给模型，原值出门 = 每天上百次 | test_只说出城市不说坐标 |
| 过期不说，没有读数 != 消失 | 沉默不能当负面 | test_过期就不说 |

## 10. 天气跟着他走

    有位置 -> 用位置推出来的 adcode
    没位置 -> GITS_AMAP_ADCODE（兜底）

实测证明这一步必要：机房 IP 是杭州，人在温州。现在 B 段是
"他这会儿在温州市。" + "温州市外面：晴，19 度。"

## 11. 还没做

- **场所聚类**（places 表）。有 30 m 精度就够了，但要先有几天轨迹。
- **预报**。走 facts 的窗口通道，不是读数。
- **去抖**。网络切换（WiFi <-> 流量）会造成假移动，需要"连续 N 次同向才认"。
  现在没做，因为还没有移动检测——只有"他在哪"。

## 12. 三处诚实更正（合成数据逼出来的）

### 12.1 现在是均匀采样，不是位置触发

脚本里是 "while :; do ...; sleep 900; done"——**每 15 分钟一次，是轮询**。

设计上说要"位置变了才触发"，代码里写的是定时循环，中间那段时间我还在文档
和对话里按"位置触发"讨论采样偏差。三个地方不一致，以代码为准。

后果：每条定位代表的时长都约等于 15 分钟，所以时间加权目前几乎是恒等变换。
**加权是对的，只是现在还没有东西可纠。** 等真的换成位置触发（Android 的
requestLocationUpdates(minDistance=...)，需要自己写一个最小 APK）它才会起作用。

### 12.2 termux-location 0.60 不返回定位时间

它返回这些字段，**没有 time**：

    latitude longitude altitude accuracy vertical_accuracy
    bearing speed elapsedMs provider

没有时间戳，服务器就只能拿收件时间当定位时间。后果是确定的：
手机离线三小时、恢复后补报一条，账上会显示"刚刚"。

现在手机端自己盖章（脚本里用 date +%s%3N 拼一个 phone_ts 进 JSON）。
phone_ts 是手机时钟。手机时钟不准没关系——我们关心的不是绝对时刻，
而是"这条定位是多久以前取的"，那是个差值。

### 12.3 elapsedMs = 5 说明返回的是缓存

termux-location 默认 -r once 会请求一次新定位，但如果定位服务已经有一份
最近的缓存，它会**立刻返回那份**——elapsedMs 是唯一的线索（实测 5 ms）。

所以 payload 里带 elapsed_ms 和 cached（elapsed_ms < 50）。缓存定位不是错，
但它的年龄未知，不能和刚取的定位混为一谈。

## 13. 特征工程：为什么不 AutoFE

AutoFE（featuretools / 自动特征生成）的前提是**样本量能压住搜索空间**。
我们现在：4 个特征、几千行、几十个正例。在这种规模上自动搜索只会找到噪声，
而且找到的东西没法审计——而审计正是这套系统的硬要求。

能做、也值得做的是**在固定的小候选集上做选择**：

    候选    小时 sin/cos、周末、距上次消息（分桶）、上次是谁先开口、场所、精度
    方法    滚动起点 CV，逐个加入，看 Brier
    成本    一次循环，几秒
    产出    "这个特征值不值得"，而不是"这个模型好不好"

这和 AutoFE 的区别不是自动化程度，是**搜索空间有边界**。边界内的搜索可以审计。

## 14. 入口：唯一对公网开的那道门

手机上报有两条通道：SSH（Termux 那条）和 HTTPS（给 app 用）。两条都落到
world/loc.py 的同一个入口，所以它们不会分叉。

    路径    只认 POST /loc。其他一律 404，**空 body**——不回版本号、不回错误信息，
            甚至不告诉你路由在不在。
    签名    HMAC-SHA256(secret, ts|body)，+/-5 分钟窗口
    重放    同一组 (ts, sig) 只接受一次
    报文    <= 512 B
    限流    每来源 IP 30 次/分钟

它不是"另一个 API"，它是门。扫到端口的人拿不到任何东西。

### 14.1 为什么不用 80/443

大陆 ECS 对**未备案域名**会拦 80/443。走 8443 一次避开两件事：
备案检查，以及"要不要开 80 做 HTTP-01"。

### 14.2 证书：DNS-01，不碰 80

域名 foxtest.icu 在 Cloudflare 上，DNS-01 用它的 API 签，不需要任何入站端口：

    certbot certonly --dns-cloudflare \
      --dns-cloudflare-credentials /root/.secrets/cf.ini \
      -d loc.foxtest.icu --non-interactive

Cloudflare 那边 A 记录必须是**灰云（DNS only）**。橙云会把流量引到 Cloudflare，
位置数据就多经过一个厂商——这条是有意的，不是配置疏忽。

### 14.3 证书不能直接给 her 读

/etc/letsencrypt 是 root 700。所以用 certbot 的 deploy hook 拷一份过去：

    /etc/letsencrypt/live/loc.foxtest.icu/fullchain.pem
      -> /var/lib/gits/shared/tls/loc.crt   640 her:gits
    /etc/letsencrypt/live/loc.foxtest.icu/privkey.pem
      -> /var/lib/gits/shared/tls/loc.key   600 her:gits
    然后 systemctl restart gits-ingress

放在 renewal-hooks/deploy/ 下，续期时自动执行。

### 14.4 多线程 + sqlite：每个请求一条连接

入口是多线程的，而 sqlite 连接不能跨线程。第一版把 DB 放在 server.state 里，
端到端测试直接返回 404——**异常被吞掉了，看起来像"路由不对"**。

这也是为什么 do_POST 里那个 except 只回 404 不打印：对外不回内容是对的，
但调试时必须能从服务端日志看到。现在这条没有日志——**是个待补的洞**。

## 15. 切换到 APK：Termux 那条已经停了

    停掉的做法（可逆）：
      mv ~/.termux/boot/01-gits-loc.sh  ~/gits-loc-boot.sh.disabled    # 开机不复活
      am force-stop com.termux                                          # 停当前循环

脚本都留着，.disabled 后缀只是挪出了 boot 目录。要回退就挪回去、重启手机。

### 为什么必须停

两条通道同时跑的那十几分钟里，账上出现了同一秒的两条：

    id=10  ts=1795680350410  cached=True     <- app，用的 last-known
    id=11  ts=1795680350339  cached=False    <- Termux

坐标差几米、时间差 71 毫秒，**幂等键不同，所以两条都落了**。它们都「对」，
但轨迹上多了一个点。这类重复不会报错，只会让后面所有基于计数的东西慢慢偏。

### 切换验证

停掉 Termux 之后手动触发一次 app：

    id=12  ts=1795680948366  cached=False    <- app 自己的新定位

### 保底留了什么

    SSH 受限命令     /root/.ssh/authorized_keys 里那条 gits-phone 还在
    phone 私钥       ~/.ssh/gits_ed25519 还在
    脚本             gits-loc.sh / gits-loc-loop.sh 都在（只是不开机自启）

app 出问题时，把 boot 脚本挪回去重启手机就能接上，不需要重新配任何东西。
## 16. 移动数据下 TLS 被重置：还没解决，但范围收窄了

### 现象

    app       SSLHandshakeException: connection closed / SocketException: Connection reset
    Termux    curl 每次都成功（TLSv1.3，证书校验通过）
    httpbin   app 打 https://httpbin.org/post 返回 200 —— **app 的 TLS 栈没问题**
    服务端    [ingress] TLS 握手失败 from <手机IP>: Closed before TLS handshake with data in recv buffer

客户端看到 RST、服务端看到 EOF——两边同时被切断，这是**路径上有人**的典型形状。

### 排除掉的

| 怀疑 | 怎么排除的 |
|---|---|
| 证书链 | 只发 3 条（leaf + YR1 + root）；curl 校验通过 |
| 端口不通 | Termux curl 到 8443 成功，tcp-ok |
| VPN / Clash | 停掉 Clash、tun0 消失后仍然失败 |
| 数据节省 / 联网控制 | data_saver 关，netpolicy UID 10341 effective=NONE |
| INTERNET 权限 | granted=true |
| TLS 版本 | 服务端锁到 1.2 也失败 |
| HTTP 栈 | 换成裸 SSLSocket 也失败 |
| 服务端单线程握手 | 已修（握手挪到工作线程），失败形状不变 |

### 还没排除的

**运营商路径上的 DPI。** 指向它的证据是时间线：app 唯一成功的那三条（id=10/11/12，16:05-16:16）
是在 Clash 开着的时候；关掉之后一次都没成功过。curl 的 ClientHello 是常见的指纹，
Conscrypt 的带着后量子密钥分享（MLKEM），有些 DPI 盒子会对不认识的扩展直接回 RST。

### 下一步（两条，都要你点头）

1. **换 443**：SG 放行 443，服务改成监听 443（systemd 里加
   AmbientCapabilities=CAP_NET_BIND_SERVICE）。DPI 对 443 通常最宽松。
   风险：阿里云对未备案域名的 80/443 有拦截，可能直接不通——一测就知道。
2. **Cloudflare 橙云**：客户端连 443 到 Cloudflare，Cloudflare 回源到 8443。
   不用改 SG，立刻能验证 DPI 猜想；代价是位置数据经过 Cloudflare 一跳。

我的建议：先试 1（数据不经第三方），不行再试 2 验证猜想。
## 17. 挂上 Cloudflare 之后：TLS 换了 nginx 终结

### 为什么换

BoringSSL 系的客户端（Android Conscrypt、Cloudflare 边缘）握手全失败，
而 OpenSSL 系的（curl/openssl）一直成功。两个 BoringSSL 客户端对同一个 Python/OpenSSL
服务端都是「发完 ClientHello 就断开」，服务端看到 `Closed before TLS handshake with data in recv buffer`。

换成 nginx 终结 TLS 后这条不用再查下去了——**TLS 是标准件，不该自己实现**。

    客户端 --TLS--> nginx:443 / :8443 --HTTP--> 127.0.0.1:8444 ingress（明文，只在本机）

### 一个必须记住的副作用

挂上反向代理之后，ingress 看到的所有对端都是 127.0.0.1。
按它限流等于**全世界共用一个桶**——一个人就能把别人锁在外面。
所以：只有在连接来自本机时（127.0.0.1/::1）才信 X-Forwarded-For，
因为只有本机的 nginx 能写这个头。

### Cloudflare 那边

    A 记录橙云      解析到 104.21.49.16 / 172.67.158.10（还有 IPv6）
    Universal SSL   已签发，CN=foxtest.icu（覆盖 *.foxtest.icu）
    客户端用 443    Cloudflare 回源也走 443 —— 所以源站必须听 443
    客户端用 8443   Cloudflare 回源仍然 443（备用端口只对入站生效），于是 525

结论：**橙云 + 源站 443** 是唯一不折腾的组合。