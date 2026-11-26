package com.botian.gitsloc;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.location.Location;
import android.location.LocationManager;

import org.json.JSONObject;

/**
 * 定位回调。**由系统派发**，不依赖进程活着。
 *
 * 时间戳用 Location.getTime()——那是 provider 给的**定位时刻**，
 * 不是"我们收到它的时刻"。缓存定位的 getTime() 是旧的，正好能识别出来。
 */
public class LocReceiver extends BroadcastReceiver {

    /** 超过这个年龄的定位就算缓存，不是刚取的。 */
    private static final long CACHED_AGE_MS = 60_000L;

    @Override
    public void onReceive(Context context, Intent intent) {
        Location loc = null;
        try {
            loc = intent.getParcelableExtra(LocationManager.KEY_LOCATION_CHANGED);
        } catch (Exception ignored) {
        }
        if (loc == null) loc = lastKnown(context);
        if (loc == null) {
            android.util.Log.i("gitsloc", "没有定位，跳过");
            return;
        }

        long now = System.currentTimeMillis();
        long fix = loc.getTime() > 0 ? loc.getTime() : now;
        long age = Math.max(0, now - fix);

        try {
            JSONObject o = new JSONObject();
            o.put("latitude", loc.getLatitude());
            o.put("longitude", loc.getLongitude());
            o.put("accuracy", loc.getAccuracy());
            o.put("provider", loc.getProvider() == null ? "unknown" : loc.getProvider());
            o.put("phone_ts", fix);
            o.put("report_ts", now);
            o.put("elapsed_ms", age);
            o.put("cached", age > CACHED_AGE_MS);
            String body = o.toString();
            android.util.Log.i("gitsloc", "上报 age=" + age + "ms " + loc.getProvider());
            if (body.getBytes("UTF-8").length > 500) return;
            // 广播接收器跑在主线程，网络 I/O 必须让出去。
            final PendingResult pr = goAsync();
            final Context ctx = context.getApplicationContext();
            new Thread(() -> {
                try {
                    Uploader.submit(ctx, body);
                } finally {
                    pr.finish();
                }
            }).start();
        } catch (Exception ignored) {
        }
    }

    private static Location lastKnown(Context c) {
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) return null;
        for (String p : new String[]{LocationManager.NETWORK_PROVIDER,
                LocationManager.GPS_PROVIDER, LocationManager.PASSIVE_PROVIDER}) {
            try {
                Location l = lm.getLastKnownLocation(p);
                if (l != null) return l;
            } catch (SecurityException ignored) {
            }
        }
        return null;
    }
}
