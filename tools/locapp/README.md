# 位置上报（Android，24 KB）

装一次、授权一次、之后不用打开。**没有 Gradle，没有依赖。**

## 为什么不用 Termux 的循环

Termux 那边是 `while :; do ...; sleep 900; done`——**轮询**。
这个 app 用 `LocationManager.requestLocationUpdates(minTime, minDistance, PendingIntent)`：

    PendingIntent 由系统持有，app 进程死掉也照样收到回调
    minDistance=200m —— 站着不动系统不叫我们

顺带解决 Termux 上的三个坑：幽灵进程杀手、wake-lock、开机脚本的 $HOME 为空。

## 参数

    network   5 分钟 / 200 米    便宜，主力
    gps      10 分钟 / 500 米    更贵，门槛更高
    心跳      4 小时              没有它，'最后一条很旧'分不清是没动还是死了

## 时间戳

用 `Location.getTime()`——provider 给的**定位时刻**，不是我们收到它的时刻。
缓存定位的 getTime() 是旧的，正好能识别出来：`cached = (now - getTime()) > 60s`。

## 本地不存历史

只存没送达的那几条（最多 50），超出丢最旧的。app 挂了就没了。
和'我们不追踪，我们接收'一致。

## 构建

    ./build.sh

需要 Android SDK 的 build-tools 34 + platform-34。第一次会生成 keystore.jks
（不进仓库）。产物 24 KB。

## 装机（可以全脚本化）

    adb install -r gitsloc.apk
    adb shell pm grant com.botian.gitsloc android.permission.ACCESS_FINE_LOCATION
    adb shell pm grant com.botian.gitsloc android.permission.ACCESS_COARSE_LOCATION
    adb shell pm grant com.botian.gitsloc android.permission.ACCESS_BACKGROUND_LOCATION
    adb shell dumpsys deviceidle whitelist +com.botian.gitsloc
    adb shell am start -n com.botian.gitsloc/.MainActivity \
        --es secret <GITS_INGRESS_SECRET> --ez start true --ez once true

最后一条把密钥写进 SharedPreferences、启动注册、并立刻上报一次。

## 还差的

- **MIUI 自启动**：adb 改不了（`appops set AUTO_START` 报 Unknown operation）。
  要手动在 设置 → 应用管理 → 位置上报 → 自启动 打开，否则重启后注册不会恢复。