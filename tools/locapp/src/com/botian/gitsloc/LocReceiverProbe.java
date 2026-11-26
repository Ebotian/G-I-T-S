package com.botian.gitsloc;

import android.content.Context;
import android.location.Location;
import android.location.LocationManager;
import android.content.Intent;

/** 手动触发一次：走和真实回调完全相同的代码路径。 */
final class LocReceiverProbe {
    static void fire(Context c) {
        Intent i = new Intent(c, LocReceiver.class).setAction(Cfg.ACTION_LOC);
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        Location loc = null;
        if (lm != null) {
            for (String p : new String[]{LocationManager.NETWORK_PROVIDER,
                    LocationManager.GPS_PROVIDER, LocationManager.PASSIVE_PROVIDER}) {
                try {
                    Location l = lm.getLastKnownLocation(p);
                    if (l != null) { loc = l; break; }
                } catch (SecurityException ignored) {
                }
            }
        }
        if (loc != null) i.putExtra(LocationManager.KEY_LOCATION_CHANGED, loc);
        c.sendBroadcast(i);
    }
}
