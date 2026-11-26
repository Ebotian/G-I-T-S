package com.botian.gitsloc;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Mac;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
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

    /**
     * 一次同步投递。**不走 HttpsURLConnection。**
     *
     * 用裸 SSLSocket 手写 HTTP/1.1。原因不是偏好，是被逼的：
     * HttpURLConnection 在这台机器上稳定报 SSLHandshakeException /
     * Connection reset，而同一台手机上 Termux 的 curl 每次都成功——
     * 说明网络和证书都没问题，问题出在那层 HTTP 栈（ALPN、连接复用、
     * 预连接探测，任何一个都可能是原因，而且都不可控）。
     *
     * 裸 socket 的行为是确定的：连、握、写、读一行状态码、关。
     */
    static boolean post(Context c, String body) {
        SSLSocket sock = null;
        try {
            String secret = Cfg.secret(c);
            if (secret.isEmpty()) {
                Cfg.note(c, "没填密钥");
                return false;
            }
            URL u = new URL(Cfg.url(c));
            String host = u.getHost();
            int port = u.getPort() > 0 ? u.getPort() : 443;
            String path = u.getPath().isEmpty() ? "/" : u.getPath();
            long ts = System.currentTimeMillis();
            byte[] payload = body.getBytes(StandardCharsets.UTF_8);
            String sig = hmac(secret, ts + "|" + body);

            SSLSocketFactory f = (SSLSocketFactory) SSLSocketFactory.getDefault();
            sock = (SSLSocket) f.createSocket();
            sock.connect(new InetSocketAddress(host, port), 15000);
            sock.setSoTimeout(20000);
            sock.startHandshake();

            String head = "POST " + path + " HTTP/1.1\r\n"
                    + "Host: " + host + "\r\n"
                    + "Content-Type: application/json\r\n"
                    + "X-Ts: " + ts + "\r\n"
                    + "X-Sig: " + sig + "\r\n"
                    + "Content-Length: " + payload.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream os = sock.getOutputStream();
            os.write(head.getBytes(StandardCharsets.UTF_8));
            os.write(payload);
            os.flush();

            BufferedReader r = new BufferedReader(
                    new InputStreamReader(sock.getInputStream(), StandardCharsets.UTF_8));
            String status = r.readLine();
            int code = 0;
            if (status != null && status.startsWith("HTTP/")) {
                String[] parts = status.split(" ");
                if (parts.length > 1) code = Integer.parseInt(parts[1]);
            }
            if (code == 204) {
                Cfg.stamp(c, "ok");
                Cfg.note(c, "");
                c.getSharedPreferences("gits", Context.MODE_PRIVATE).edit()
                        .putInt("retry_n", 0).apply();
                return true;
            }
            Cfg.note(c, "HTTP " + code);
            android.util.Log.w("gitsloc", "HTTP " + code);
            return false;
        } catch (Exception e) {
            Throwable root = e;
            while (root.getCause() != null) root = root.getCause();
            String msg = e.getClass().getSimpleName() + ": " + e.getMessage()
                    + " <- " + root.getClass().getSimpleName() + ": " + root.getMessage();
            Cfg.note(c, msg.length() > 300 ? msg.substring(0, 300) : msg);
            android.util.Log.w("gitsloc", msg);
            return false;
        } finally {
            if (sock != null) {
                try {
                    sock.close();
                } catch (Exception ignored) {
                }
            }
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
