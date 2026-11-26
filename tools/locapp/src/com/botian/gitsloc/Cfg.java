package com.botian.gitsloc;

import android.content.Context;
import android.content.SharedPreferences;

/** 配置：一个地址、一把密钥。装完填一次，之后不再碰。 */
final class Cfg {
    static final String DEFAULT_URL = "https://loc.foxtest.icu:8443/loc";
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
