package com.notifysync;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.Base64;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.text.InputType;
import android.widget.Toast;

import androidx.core.app.ActivityCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String PREFS_NAME = "NotifySyncPrefs";
    private static final String PREF_SERVER_IP = "server_ip";
    private static final String PREF_SERVER_PORT = "server_port";
    private static final String PREF_AUTH_TOKEN = "auth_token";
    private static final String PREF_CRYPTO_KEY = "crypto_key";
    private static final String PREF_FIRST_LAUNCH_HINT_SHOWN = "first_launch_hint_shown";
    private static final int REQUEST_POST_NOTIFICATIONS = 1001;
    private static final String LOCAL_CHANNEL_ID = "notifysync_local";

    private EditText etIp;
    private EditText etPort;
    private EditText etToken;
    private EditText etCryptoKey;
    private TextView tvStatus;
    private TextView tvTestResult;
    private Button btnRequestPermission;
    private Button btnTest;
    private Button btnGenerateToken;
    private Button btnGenerateCryptoKey;
    private Button btnToggleToken;
    private Button btnToggleCryptoKey;
    private Button btnClearToken;
    private Button btnClearCryptoKey;
    private ExecutorService executor;
    private final Handler autosaveHandler = new Handler(Looper.getMainLooper());
    private Runnable pendingAutosave;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        etIp = findViewById(R.id.et_ip);
        etPort = findViewById(R.id.et_port);
        etToken = findViewById(R.id.et_token);
        etCryptoKey = findViewById(R.id.et_crypto_key);
        tvStatus = findViewById(R.id.tv_status);
        tvTestResult = findViewById(R.id.tv_test_result);
        btnRequestPermission = findViewById(R.id.btn_request_permission);
        btnTest = findViewById(R.id.btn_test);
        btnGenerateToken = findViewById(R.id.btn_generate_token);
        btnGenerateCryptoKey = findViewById(R.id.btn_generate_crypto_key);
        btnToggleToken = findViewById(R.id.btn_toggle_token);
        btnToggleCryptoKey = findViewById(R.id.btn_toggle_crypto_key);
        btnClearToken = findViewById(R.id.btn_clear_token);
        btnClearCryptoKey = findViewById(R.id.btn_clear_crypto_key);
        executor = Executors.newSingleThreadExecutor();

        // 加载保存的设置
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        etIp.setText(prefs.getString(PREF_SERVER_IP, "192.168.43.1"));
        etPort.setText(String.valueOf(prefs.getInt(PREF_SERVER_PORT, 8787)));
        etToken.setText(prefs.getString(PREF_AUTH_TOKEN, ""));
        etCryptoKey.setText(prefs.getString(PREF_CRYPTO_KEY, ""));

        btnRequestPermission.setOnClickListener(v -> requestNotificationPermission());
        btnTest.setOnClickListener(v -> testConnectionAndNotify());
        btnGenerateToken.setOnClickListener(v -> {
            etToken.setText(generateRandomSecret(24));
            autoSaveNow(true);
        });
        btnGenerateCryptoKey.setOnClickListener(v -> {
            etCryptoKey.setText(generateRandomSecret(32));
            autoSaveNow(true);
        });
        btnToggleToken.setOnClickListener(v -> togglePasswordVisibility(etToken, btnToggleToken));
        btnToggleCryptoKey.setOnClickListener(v -> togglePasswordVisibility(etCryptoKey, btnToggleCryptoKey));
        btnClearToken.setOnClickListener(v -> etToken.setText(""));
        btnClearCryptoKey.setOnClickListener(v -> etCryptoKey.setText(""));

        setupAutoSave();

        createLocalNotificationChannel();
        ensurePostNotificationsPermission();

        // 启动服务
        startService(new Intent(this, NotificationService.class));

        maybeShowFirstLaunchHint();
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateStatus();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (pendingAutosave != null) {
            autosaveHandler.removeCallbacks(pendingAutosave);
        }
        if (executor != null) {
            executor.shutdown();
        }
    }

    private void maybeShowFirstLaunchHint() {
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        boolean shown = prefs.getBoolean(PREF_FIRST_LAUNCH_HINT_SHOWN, false);
        if (shown) {
            return;
        }

        new android.app.AlertDialog.Builder(this)
            .setTitle("首次使用提醒")
            .setMessage("为确保通知稳定转发，请完成以下设置：\n\n"
                + "1. 开启应用自启动\n"
                + "2. 关闭电池优化/省电限制（设为无限制）\n\n"
                + "不同手机路径可能不同，通常在：\n"
                + "设置 > 应用管理 > NotifySync > 电池/省电策略")
            .setPositiveButton("知道了", (d, w) -> {
                prefs.edit().putBoolean(PREF_FIRST_LAUNCH_HINT_SHOWN, true).apply();
                d.dismiss();
            })
            .setCancelable(false)
            .show();
    }

    private void updateStatus() {
        boolean listenerOk = NotificationService.hasPermission(this);
        boolean postOk = hasPostNotificationsPermission();

        if (listenerOk && postOk) {
            tvStatus.setText("状态: 已授权 ✓ (监听+发送)");
            tvStatus.setTextColor(0xFF00AA00);
            btnRequestPermission.setText("重新授权");
        } else {
            String detail = "";
            if (!listenerOk && !postOk) {
                detail = "（缺监听权限+通知发送权限）";
            } else if (!listenerOk) {
                detail = "（缺监听权限）";
            } else {
                detail = "（缺通知发送权限）";
            }
            tvStatus.setText("状态: 未完整授权 ✗ " + detail);
            tvStatus.setTextColor(0xFFAA0000);
            btnRequestPermission.setText("去授权");
        }
    }

    private boolean hasPostNotificationsPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return true;
        }
        return ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED;
    }

    private void requestNotificationPermission() {
        ensurePostNotificationsPermission();

        if (!NotificationService.hasPermission(this)) {
            Intent intent = new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS);
            startActivity(intent);
            Toast.makeText(this, "请找到 NotifySync 并开启通知访问权限", Toast.LENGTH_LONG).show();
            return;
        }

        Toast.makeText(this, "通知访问权限已开启", Toast.LENGTH_SHORT).show();
    }

    private void ensurePostNotificationsPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            maybeShowLocalDebugNotification();
            return;
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED) {
            maybeShowLocalDebugNotification();
            return;
        }

        ActivityCompat.requestPermissions(
            this,
            new String[]{Manifest.permission.POST_NOTIFICATIONS},
            REQUEST_POST_NOTIFICATIONS
        );
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_POST_NOTIFICATIONS) {
            return;
        }

        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (granted) {
            Toast.makeText(this, "通知发送权限已开启", Toast.LENGTH_SHORT).show();
            maybeShowLocalDebugNotification();
        } else {
            Toast.makeText(this, "通知发送权限未开启，可能导致通知栏无提示", Toast.LENGTH_LONG).show();
        }
    }

    private void createLocalNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }

        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager == null) {
            return;
        }

        NotificationChannel existing = manager.getNotificationChannel(LOCAL_CHANNEL_ID);
        if (existing != null) {
            return;
        }

        NotificationChannel channel = new NotificationChannel(
            LOCAL_CHANNEL_ID,
            "NotifySync 本地通知",
            NotificationManager.IMPORTANCE_HIGH
        );
        channel.setDescription("用于 NotifySync 调试与状态提示");
        manager.createNotificationChannel(channel);
    }

    private void maybeShowLocalDebugNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            return;
        }

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, LOCAL_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("NotifySync")
            .setContentText("通知功能已启用，可在此测试通知栏显示")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true);

        NotificationManagerCompat.from(this).notify(10001, builder.build());
    }

    private void setupAutoSave() {
        TextWatcher watcher = new TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {
            }

            @Override
            public void onTextChanged(CharSequence s, int start, int before, int count) {
                scheduleAutoSave();
            }

            @Override
            public void afterTextChanged(Editable s) {
            }
        };

        etIp.addTextChangedListener(watcher);
        etPort.addTextChangedListener(watcher);
        etToken.addTextChangedListener(watcher);
        etCryptoKey.addTextChangedListener(watcher);

        etIp.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) autoSaveNow(false);
        });
        etPort.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) autoSaveNow(false);
        });
        etToken.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) autoSaveNow(false);
        });
        etCryptoKey.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) autoSaveNow(false);
        });
    }

    private void scheduleAutoSave() {
        if (pendingAutosave != null) {
            autosaveHandler.removeCallbacks(pendingAutosave);
        }
        pendingAutosave = () -> autoSaveNow(false);
        autosaveHandler.postDelayed(pendingAutosave, 500);
    }

    private void autoSaveNow(boolean showToast) {
        String ip = etIp.getText().toString().trim();
        String portStr = etPort.getText().toString().trim();
        String token = etToken.getText().toString().trim();
        String cryptoKey = etCryptoKey.getText().toString().trim();

        if (!validateInputs(ip, portStr)) {
            return;
        }

        int port = Integer.parseInt(portStr);
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);

        String oldIp = prefs.getString(PREF_SERVER_IP, "192.168.43.1");
        int oldPort = prefs.getInt(PREF_SERVER_PORT, 8787);
        String oldToken = prefs.getString(PREF_AUTH_TOKEN, "");
        String oldCrypto = prefs.getString(PREF_CRYPTO_KEY, "");

        boolean changed = !ip.equals(oldIp)
            || port != oldPort
            || !token.equals(oldToken)
            || !cryptoKey.equals(oldCrypto);

        if (!changed) {
            return;
        }

        prefs.edit()
            .putString(PREF_SERVER_IP, ip)
            .putInt(PREF_SERVER_PORT, port)
            .putString(PREF_AUTH_TOKEN, token)
            .putString(PREF_CRYPTO_KEY, cryptoKey)
            .apply();

        stopService(new Intent(this, NotificationService.class));
        startService(new Intent(this, NotificationService.class));

        if (showToast) {
            Toast.makeText(this, "设置已自动保存", Toast.LENGTH_SHORT).show();
        }
    }

    private String generateRandomSecret(int byteLen) {
        byte[] bytes = new byte[byteLen];
        new SecureRandom().nextBytes(bytes);
        return Base64.encodeToString(bytes, Base64.NO_WRAP);
    }

    private void togglePasswordVisibility(EditText editText, Button btn) {
        int inputType = editText.getInputType();
        boolean isHidden = (inputType & InputType.TYPE_TEXT_VARIATION_PASSWORD) != 0;
        if (isHidden) {
            editText.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
            btn.setText("隐藏");
        } else {
            editText.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            btn.setText("显示");
        }
        editText.setSelection(editText.getText().length());
    }

    private boolean validateInputs(String ip, String portStr) {
        if (ip.isEmpty()) {
            etIp.setError("请输入IP地址");
            return false;
        }

        try {
            int port = Integer.parseInt(portStr);
            if (port < 1 || port > 65535) {
                etPort.setError("端口范围 1-65535");
                return false;
            }
        } catch (NumberFormatException e) {
            etPort.setError("无效端口");
            return false;
        }

        return true;
    }

    private void testConnectionAndNotify() {
        String ip = etIp.getText().toString().trim();
        String portStr = etPort.getText().toString().trim();

        if (!validateInputs(ip, portStr)) {
            return;
        }

        btnTest.setEnabled(false);
        tvTestResult.setText("正在测试连接...");

        executor.execute(() -> {
            String result;
            try {
                int port = Integer.parseInt(portStr);
                String base = "http://" + ip + ":" + port;

                HttpURLConnection healthConn = (HttpURLConnection) new URL(base + "/health").openConnection();
                healthConn.setRequestMethod("GET");
                healthConn.setConnectTimeout(3000);
                healthConn.setReadTimeout(3000);
                int healthCode = healthConn.getResponseCode();
                if (healthCode != 200) {
                    result = "连接失败：/health 返回 " + healthCode;
                } else {
                    HttpURLConnection testConn = (HttpURLConnection) new URL(base + "/test").openConnection();
                    testConn.setRequestMethod("GET");
                    testConn.setConnectTimeout(3000);
                    testConn.setReadTimeout(3000);
                    int testCode = testConn.getResponseCode();

                    if (testCode == 200) {
                        String body = "";
                        try (BufferedReader reader = new BufferedReader(
                            new InputStreamReader(testConn.getInputStream())
                        )) {
                            String line = reader.readLine();
                            if (line != null) body = line;
                        }
                        result = "测试成功：电脑端已触发测试通知\n" + body;
                    } else {
                        result = "连接成功，但测试通知触发失败：/test 返回 " + testCode;
                    }
                }
            } catch (Exception e) {
                result = "测试失败：" + e.getMessage();
            }

            String finalResult = result;
            runOnUiThread(() -> {
                tvTestResult.setText(finalResult);
                btnTest.setEnabled(true);
            });
        });
    }
}

