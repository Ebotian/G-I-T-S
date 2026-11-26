package com.botian.gitsloc;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.location.Criteria;
import android.location.LocationManager;

/** 注册定位回调。**PendingIntent 由系统持有**——app 进程死掉也照样收到。 */
final class Registrar {

    /** 5 分钟 / 200 米：便宜、够用。位移小于 200 米系统不会叫我们。 */
    private static final long NET_MIN_TIME = 5 * 60_000L;
    private static final float NET_MIN_DIST = 200f;
    /** GPS 更贵，门槛也更高。 */
    private static final long GPS_MIN_TIME = 10 * 60_000L;
    private static final float GPS_MIN_DIST = 500f;
    /** 心跳：4 小时。没有它，"最后一条很旧"分不清是没动还是死了。 */
    private static final long HEARTBEAT_MS = 4 * 3_600_000L;

    static PendingIntent locIntent(Context c) {
        Intent i = new Intent(c, LocReceiver.class).setAction(Cfg.ACTION_LOC);
        return PendingIntent.getBroadcast(c, 1001, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
    }

    static void start(Context c) {
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) return;
        PendingIntent pi = locIntent(c);
        try {
            if (lm.getAllProviders().contains(LocationManager.NETWORK_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER,
                        NET_MIN_TIME, NET_MIN_DIST, pi);
            }
        } catch (SecurityException ignored) {
        }
        try {
            if (lm.getAllProviders().contains(LocationManager.GPS_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.GPS_PROVIDER,
                        GPS_MIN_TIME, GPS_MIN_DIST, pi);
            }
        } catch (SecurityException ignored) {
        }
        startHeartbeat(c);
    }

    private static void startHeartbeat(Context c) {
        AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        Intent i = new Intent(c, LocReceiver.class).setAction(Cfg.ACTION_HEARTBEAT);
        PendingIntent pi = PendingIntent.getBroadcast(c, 1002, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        am.setInexactRepeating(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                HEARTBEAT_MS, HEARTBEAT_MS, pi);
    }

    /** 用一次定位：手动测试用。 */
    static String lastKnown(Context c) {
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) return null;
        Criteria cr = new Criteria();
        cr.setAccuracy(Criteria.ACCURACY_COARSE);
        try {
            String p = lm.getBestProvider(cr, true);
            return p;
        } catch (Exception e) {
            return null;
        }
    }
}
