package com.botian.gitsloc;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 重启之后注册会丢，重新来一次。**这个接收器就是"无需打开"的全部秘密。** */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Registrar.start(context);
    }
}
