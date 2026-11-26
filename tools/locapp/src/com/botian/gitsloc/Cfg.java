package com.botian.gitsloc;

import android.content.Context;
import android.content.SharedPreferences;

/** 配置：一个地址、一把密钥。装完填一次，之后不再碰。 */
final class Cfg {
    // 走 Cloudflare 隧道：手机连 CF 的 443，源站主动出连到 CF。
    // 直连 8443 那条路在移动数据下会 TLS 握手失败（见 docs/research/space.md §16）。
    static final String DEFAULT_URL = "https://loc.foxtest.icu/loc";
    static final String ACTION_LOC = "com.botian.gitsloc.LOC";
    static final String ACTION_HEARTBEAT = "com.botian.gitsloc.BEAT";
    static final String ACTION_RETRY = "com.botian.gitsloc.RETRY";

    private static SharedPreferences sp(Context c) {
        return c.getSharedPreferences("gits", Context.MODE_PRIVATE);
    }

    static String url(Context c) {
        String v = sp(c).getString("url", "");
        return v.isEmpty() ? DEFAULT_URL : v;
    }

    static String secret(Context c) {
        return sp(c).getString("secret", "");
    }

    static void save(Context c, String url, String secret) {
        sp(c).edit().putString("url", url).putString("secret", secret).apply();
    }

    static void stamp(Context c, String what) {
        sp(c).edit().putLong("last_ok", System.currentTimeMillis())
                .putString("last_what", what).apply();
    }

    static void note(Context c, String err) {
        sp(c).edit().putString("last_err", err).apply();
    }

    static long lastOk(Context c) {
        return sp(c).getLong("last_ok", 0);
    }
}
