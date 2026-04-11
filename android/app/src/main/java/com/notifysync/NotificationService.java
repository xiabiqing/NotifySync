package com.notifysync;

import android.app.Notification;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.text.TextUtils;
import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

public class NotificationService extends NotificationListenerService {
    private static final String TAG = "NotifySync";
    private static final String PREFS_NAME = "NotifySyncPrefs";
    private static final String PREF_SERVER_IP = "server_ip";
    private static final String PREF_SERVER_PORT = "server_port";
    private static final String PREF_AUTH_TOKEN = "auth_token";
    private static final String PREF_CRYPTO_KEY = "crypto_key";
    private static final String PREF_DIAG_LAST = "diag_last";
    private static final String PREF_DIAG_HISTORY = "diag_history";

    private ExecutorService executor;
    private String serverUrl;
    private String authToken;
    private String cryptoKey;

    public static boolean hasPermission(Context context) {
        String listeners = Settings.Secure.getString(
            context.getContentResolver(),
            "enabled_notification_listeners"
        );
        if (listeners == null) return false;
        ComponentName cn = new ComponentName(context, NotificationService.class);
        return listeners.contains(cn.flattenToString());
    }

    @Override
    public void onCreate() {
        super.onCreate();
        executor = Executors.newSingleThreadExecutor();
        updateServerUrl();
        Log.i(TAG, "Service created");
        writeDiag("service created, target=" + serverUrl);
    }

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        Log.i(TAG, "Notification listener connected");
        updateServerUrl();
        writeDiag("listener connected, target=" + serverUrl);
    }

    @Override
    public void onListenerDisconnected() {
        super.onListenerDisconnected();
        Log.w(TAG, "Notification listener disconnected");
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (executor != null) {
            executor.shutdown();
        }
    }

    private void updateServerUrl() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String ip = prefs.getString(PREF_SERVER_IP, "192.168.43.1");
        int port = prefs.getInt(PREF_SERVER_PORT, 8787);
        authToken = prefs.getString(PREF_AUTH_TOKEN, "");
        cryptoKey = prefs.getString(PREF_CRYPTO_KEY, "");
        serverUrl = "http://" + ip + ":" + port + "/notify";
    }

    private void writeDiag(String message) {
        String line = System.currentTimeMillis() + " | " + message;
        Log.i(TAG, "DIAG " + message);
        try {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            String old = prefs.getString(PREF_DIAG_HISTORY, "");
            String merged = old.isEmpty() ? line : (old + "\n" + line);
            if (merged.length() > 4000) {
                merged = merged.substring(merged.length() - 4000);
            }
            prefs.edit()
                .putString(PREF_DIAG_LAST, line)
                .putString(PREF_DIAG_HISTORY, merged)
                .apply();
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        String pkg = sbn.getPackageName();
        Log.d(TAG, "onNotificationPosted pkg=" + pkg + ", key=" + sbn.getKey());

        if (shouldSkip(sbn)) {
            Log.d(TAG, "Skipped notification from pkg=" + pkg);
            writeDiag("skip pkg=" + pkg + " key=" + sbn.getKey());
            return;
        }

        NotificationData data = extractNotificationData(sbn);
        if (TextUtils.isEmpty(data.title) && TextUtils.isEmpty(data.text) && TextUtils.isEmpty(data.subText)) {
            data.text = "(空内容通知)";
        }

        Log.d(TAG, "Forwarding notification app=" + data.appName + ", title=" + data.title);
        writeDiag("captured app=" + data.appName + " title=" + safeSnippet(data.title)
            + " text=" + safeSnippet(data.text));
        sendToServer(data);
    }

    @Override
    public void onNotificationRemoved(StatusBarNotification sbn) {
        // 通知移除时不发送，保持简单
        Log.d(TAG, "Notification removed: " + sbn.getKey());
    }

    private boolean shouldSkip(StatusBarNotification sbn) {
        String pkg = sbn.getPackageName();

        // 跳过系统通知
        if (pkg.equals("android") || pkg.equals("com.android.systemui")) {
            return true;
        }

        // 跳过自己的通知
        if (pkg.equals(getPackageName())) {
            return true;
        }

        Notification n = sbn.getNotification();

        // 跳过前台服务通知
        if ((n.flags & Notification.FLAG_FOREGROUND_SERVICE) != 0) {
            return true;
        }

        // 暂不按 ongoing 过滤，避免厂商 ROM/应用实现差异导致误杀消息通知
        return false;
    }

    private NotificationData extractNotificationData(StatusBarNotification sbn) {
        NotificationData data = new NotificationData();
        data.id = sbn.getKey();
        data.packageName = sbn.getPackageName();
        data.postTime = sbn.getPostTime();

        // 获取应用名称
        try {
            android.content.pm.PackageManager pm = getPackageManager();
            android.content.pm.ApplicationInfo ai = pm.getApplicationInfo(sbn.getPackageName(), 0);
            data.appName = pm.getApplicationLabel(ai).toString();
        } catch (Exception e) {
            data.appName = sbn.getPackageName();
        }

        // 提取通知内容（兼容 MessagingStyle / QQ 等机型差异）
        Bundle extras = sbn.getNotification().extras;

        CharSequence title = extras.getCharSequence(Notification.EXTRA_TITLE);
        if (title == null) {
            title = extras.getCharSequence(Notification.EXTRA_TITLE_BIG);
        }
        data.title = title != null ? title.toString() : "";

        CharSequence text = extras.getCharSequence(Notification.EXTRA_TEXT);
        if (text == null) {
            text = extras.getCharSequence(Notification.EXTRA_BIG_TEXT);
        }
        if (text == null) {
            CharSequence[] lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
            if (lines != null && lines.length > 0) {
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < lines.length; i++) {
                    if (lines[i] == null) continue;
                    if (sb.length() > 0) sb.append("\n");
                    sb.append(lines[i]);
                }
                text = sb.length() > 0 ? sb.toString() : null;
            }
        }
        if (text == null) {
            text = sbn.getNotification().tickerText;
        }
        data.text = text != null ? text.toString() : "";

        // 提取摘要文字
        CharSequence subText = extras.getCharSequence(Notification.EXTRA_SUB_TEXT);
        data.subText = subText != null ? subText.toString() : "";

        // 最后兜底，避免发送空通知导致 Windows 侧不显示
        if (data.title.isEmpty() && !data.text.isEmpty()) {
            data.title = data.text.length() > 30 ? data.text.substring(0, 30) + "..." : data.text;
        }

        return data;
    }

    private String safeSnippet(String src) {
        if (src == null) return "";
        String t = src.replace("\n", " ").trim();
        if (t.length() > 40) {
            return t.substring(0, 40) + "...";
        }
        return t;
    }

    private void sendToServer(NotificationData data) {
        if (executor == null || executor.isShutdown()) return;

        executor.execute(() -> {
            try {
                updateServerUrl();

                JSONObject plain = new JSONObject();
                plain.put("id", data.id);
                plain.put("appName", data.appName);
                plain.put("packageName", data.packageName);
                plain.put("title", data.title);
                plain.put("text", data.text);
                plain.put("subText", data.subText);
                plain.put("time", data.postTime);

                String payload;
                if (!TextUtils.isEmpty(cryptoKey)) {
                    payload = buildEncryptedPayload(plain.toString(), cryptoKey);
                } else {
                    payload = plain.toString();
                }

                byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);

                URL url = new URL(serverUrl);
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json; charset=UTF-8");
                conn.setRequestProperty("Content-Length", String.valueOf(bytes.length));
                if (!TextUtils.isEmpty(authToken)) {
                    conn.setRequestProperty("Authorization", "Bearer " + authToken);
                    conn.setRequestProperty("X-NotifySync-Token", authToken);
                }
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);

                try (OutputStream os = conn.getOutputStream()) {
                    os.write(bytes);
                }

                int responseCode = conn.getResponseCode();
                if (responseCode == 200) {
                    Log.d(TAG, "Notification sent: " + data.appName + " - " + data.title);
                    writeDiag("sent ok code=200 app=" + data.appName + " title=" + safeSnippet(data.title));
                } else {
                    Log.w(TAG, "Failed to send notification: " + responseCode);
                    writeDiag("send failed code=" + responseCode + " target=" + serverUrl);
                }

            } catch (Exception e) {
                Log.e(TAG, "Error sending notification", e);
                writeDiag("send exception=" + e.getClass().getSimpleName() + " msg=" + (e.getMessage() == null ? "" : safeSnippet(e.getMessage())));
            }
        });
    }

    private String buildEncryptedPayload(String plainText, String keyText) throws Exception {
        byte[] aesKey = sha256Bytes("NS-AES|" + keyText);
        byte[] hmacKey = sha256Bytes("NS-HMAC|" + keyText);

        byte[] iv = new byte[12];
        new SecureRandom().nextBytes(iv);

        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        SecretKeySpec keySpec = new SecretKeySpec(aesKey, "AES");
        GCMParameterSpec gcmSpec = new GCMParameterSpec(128, iv);
        cipher.init(Cipher.ENCRYPT_MODE, keySpec, gcmSpec);
        byte[] encrypted = cipher.doFinal(plainText.getBytes(StandardCharsets.UTF_8));

        long ts = System.currentTimeMillis() / 1000L;
        byte[] nonceBytes = new byte[8];
        new SecureRandom().nextBytes(nonceBytes);
        String nonce = Base64.encodeToString(nonceBytes, Base64.NO_WRAP);
        String ivB64 = Base64.encodeToString(iv, Base64.NO_WRAP);
        String dataB64 = Base64.encodeToString(encrypted, Base64.NO_WRAP);

        String toSign = ts + "." + nonce + "." + ivB64 + "." + dataB64;
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(hmacKey, "HmacSHA256"));
        String signB64 = Base64.encodeToString(mac.doFinal(toSign.getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);

        JSONObject wrapped = new JSONObject();
        wrapped.put("enc", "v1");
        wrapped.put("ts", ts);
        wrapped.put("nonce", nonce);
        wrapped.put("iv", ivB64);
        wrapped.put("data", dataB64);
        wrapped.put("sig", signB64);
        return wrapped.toString();
    }

    private byte[] sha256Bytes(String value) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        return digest.digest(value.getBytes(StandardCharsets.UTF_8));
    }

    static class NotificationData {
        String id;
        String packageName;
        String appName;
        String title;
        String text;
        String subText;
        long postTime;
    }
}
