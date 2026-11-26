package com.botian.gitsloc;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 补发队列。退避由 Uploader 自己排。 */
public class RetryReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Uploader.drain(context);
    }
}
