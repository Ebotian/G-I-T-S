package com.botian.gitsloc;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * 上报：签名、投递、失败重试。
 *
 * 本地**不存历史**，只存没送达的那几条（最多 50），超出丢最旧的。
 * 和"我们不追踪，我们接收"一致：app 挂了就没了，不影响"他大概在哪"。
 */
final class Uploader {

    private static final int MAX_QUEUE = 50;
    private static final long[] BACKOFF = {30_000L, 120_000L, 600_000L, 1_800_000L, 3_600_000L};

    private static File queueFile(Context c) {
        return new File(c.getFilesDir(), "queue.txt");
    }

    static void submit(Context c, String body) {
        if (!post(c, body)) {
            enqueue(c, body);
            scheduleRetry(c);
        }
    }

    /** 把队列里能送的都送掉。返回送成功几条。 */
    static int drain(Context c) {
        File f = queueFile(c);
        if (!f.exists()) return 0;
        List<String> lines = new ArrayList<>();
        try (BufferedReader r = new BufferedReader(new FileReader(f))) {
            String line;
            while ((line = r.readLine()) != null) if (!line.isEmpty()) lines.add(line);
        } catch (Exception e) {
            return 0;
        }
        int ok = 0;
        List<String> left = new ArrayList<>();
        for (String b : lines) {
            if (post(c, b)) ok++;
            else left.add(b);
        }
        try {
            if (left.isEmpty()) {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            } else {
                try (OutputStream os = new FileOutputStream(f, false)) {
                    for (String b : left) os.write((b + "\n").getBytes(StandardCharsets.UTF_8));
                }
            }
        } catch (Exception ignored) {
        }
        if (!left.isEmpty()) scheduleRetry(c);
        return ok;
    }

    private static void enqueue(Context c, String body) {
        List<String> lines = new ArrayList<>();
        File f = queueFile(c);
        try {
            if (f.exists()) {
                try (BufferedReader r = new BufferedReader(new FileReader(f))) {
                    String line;
                    while ((line = r.readLine()) != null) if (!line.isEmpty()) lines.add(line);
                }
            }
            lines.add(body);
            while (lines.size() > MAX_QUEUE) lines.remove(0);
            try (OutputStream os = new FileOutputStream(f, false)) {
                for (String b : lines) os.write((b + "\n").getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    private static void scheduleRetry(Context c) {
        AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        Intent i = new Intent(c, RetryReceiver.class).setAction(Cfg.ACTION_RETRY);
        PendingIntent pi = PendingIntent.getBroadcast(c, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        int n = c.getSharedPreferences("gits", Context.MODE_PRIVATE).getInt("retry_n", 0);
        long delay = BACKOFF[Math.min(n, BACKOFF.length - 1)];
        c.getSharedPreferences("gits", Context.MODE_PRIVATE).edit()
                .putInt("retry_n", Math.min(n + 1, BACKOFF.length - 1)).apply();
        am.set(AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + delay, pi);
    }

    /** 一次同步投递。**没有异常逃出去**——广播接收器里不能崩。 */
    static boolean post(Context c, String body) {
        HttpURLConnection conn = null;
        try {
            String secret = Cfg.secret(c);
            if (secret.isEmpty()) {
                Cfg.note(c, "没填密钥");
                return false;
            }
            long ts = System.currentTimeMillis();
            byte[] payload = body.getBytes(StandardCharsets.UTF_8);
            String sig = hmac(secret, ts + "|" + body);
            URL url = new URL(Cfg.url(c));
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(20000);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setFixedLengthStreamingMode(payload.length);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("X-Ts", Long.toString(ts));
            conn.setRequestProperty("X-Sig", sig);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(payload);
            }
            int code = conn.getResponseCode();
            if (code == 204) {
                Cfg.stamp(c, "ok");
                Cfg.note(c, "");
                c.getSharedPreferences("gits", Context.MODE_PRIVATE).edit()
                        .putInt("retry_n", 0).apply();
                return true;
            }
            Cfg.note(c, "HTTP " + code);
            return false;
        } catch (Exception e) {
            Cfg.note(c, e.getClass().getSimpleName());
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String hmac(String secret, String msg) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        byte[] d = mac.doFinal(msg.getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) sb.append(String.format("%02x", b));
        return sb.toString();
    }
}
