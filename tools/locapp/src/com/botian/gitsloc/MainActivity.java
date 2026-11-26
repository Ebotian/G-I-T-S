package com.botian.gitsloc;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 装完用一次：授权、填密钥、点开始。之后永远不用打开。
 *
 * 界面是代码搭的，没有布局文件——这个应用只有四个组件，
 * 为了它去写 res/ 只会让构建变复杂。
 */
public class MainActivity extends Activity {

    private static final int REQ_PERM = 1;
    private TextView status;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("位置上报");
        title.setTextSize(22);
        title.setPadding(0, 0, 0, pad / 2);
        root.addView(title);

        EditText url = new EditText(this);
        url.setHint(Cfg.DEFAULT_URL);
        url.setText(Cfg.url(this));
        url.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        root.addView(labelled("地址", url));

        EditText secret = new EditText(this);
        secret.setHint("服务器上的 GITS_INGRESS_SECRET");
        secret.setText(Cfg.secret(this));
        secret.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        root.addView(labelled("密钥", secret));

        Button save = new Button(this);
        save.setText("保存并开始");
        save.setOnClickListener(v -> {
            Cfg.save(this, url.getText().toString().trim(),
                    secret.getText().toString().trim());
            askPermissions();
            Registrar.start(this);
            refresh();
        });
        root.addView(save);

        Button once = new Button(this);
        once.setText("现在上报一次");
        once.setOnClickListener(v -> {
            new Thread(() -> {
                LocReceiverProbe.fire(this);
                Uploader.drain(this);
                runOnUiThread(this::refresh);
            }).start();
        });
        root.addView(once);

        Button battery = new Button(this);
        battery.setText("允许后台不被省电杀掉");
        battery.setOnClickListener(v -> {
            try {
                PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
                if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    i.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(i);
                }
            } catch (Exception ignored) {
            }
        });
        root.addView(battery);

        status = new TextView(this);
        status.setTextSize(14);
        status.setPadding(0, pad, 0, 0);
        root.addView(status);

        ScrollView sv = new ScrollView(this);
        sv.addView(root);
        setContentView(sv);

        // **可脚本化**：装完用 am start 带参数一次配好，不用手点。
        //   adb shell am start -n com.botian.gitsloc/.MainActivity \
        //       --es secret XXX --ez start true --ez once true
        Intent in = getIntent();
        if (in != null && in.getStringExtra("secret") != null) {
            Cfg.save(this, in.getStringExtra("url") == null
                            ? Cfg.DEFAULT_URL : in.getStringExtra("url"),
                     in.getStringExtra("secret"));
            url.setText(Cfg.url(this));
            secret.setText(Cfg.secret(this));
        }
        refresh();
        if (in != null && in.getBooleanExtra("start", false)) {
            askPermissions();
            Registrar.start(this);
        }
        if (in != null && in.getBooleanExtra("once", false)) {
            new Thread(() -> {
                LocReceiverProbe.fire(this);
                Uploader.drain(this);
                runOnUiThread(this::refresh);
            }).start();
        }
    }

    private LinearLayout labelled(String name, View v) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        TextView t = new TextView(this);
        t.setText(name);
        t.setTextSize(12);
        t.setTextColor(Color.GRAY);
        box.addView(t);
        box.addView(v);
        return box;
    }

    private void askPermissions() {
        if (Build.VERSION.SDK_INT >= 23) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                    Manifest.permission.POST_NOTIFICATIONS}, REQ_PERM);
        }
    }

    /** 后台定位必须单独申请：Android 11+ 要求"始终允许"，而且不能和前台权限一起弹。 */
    @Override
    public void onRequestPermissionsResult(int code, String[] p, int[] r) {
        super.onRequestPermissionsResult(code, p, r);
        if (Build.VERSION.SDK_INT >= 29) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_BACKGROUND_LOCATION}, REQ_PERM + 1);
        }
        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    private void refresh() {
        StringBuilder sb = new StringBuilder();
        boolean fine = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
        sb.append("定位权限：").append(fine ? "有" : "没有").append("\n");
        if (Build.VERSION.SDK_INT >= 29) {
            boolean bg = checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
            sb.append("后台定位：").append(bg ? "有" : "没有（必须选「始终允许」）").append("\n");
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null) {
            sb.append("省电白名单：")
                    .append(pm.isIgnoringBatteryOptimizations(getPackageName()) ? "有" : "没有")
                    .append("\n");
        }
        long last = Cfg.lastOk(this);
        sb.append("上次成功：").append(last == 0 ? "还没成功过"
                : new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA)
                .format(new Date(last))).append("\n");
        String err = getSharedPreferences("gits", MODE_PRIVATE).getString("last_err", "");
        if (err != null && !err.isEmpty()) sb.append("上次错误：").append(err).append("\n");
        status.setText(sb.toString());
    }
}
