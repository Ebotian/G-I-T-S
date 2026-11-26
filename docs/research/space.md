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
