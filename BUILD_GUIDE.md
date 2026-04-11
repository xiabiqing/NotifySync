# 构建指南

## 方法一：使用 Android Studio（推荐）

1. 下载并安装 [Android Studio](https://developer.android.com/studio)
2. 打开 `NotifySync/android` 文件夹
3. 点击 Build → Build Bundle(s) / APK(s) → Build APK(s)
4. 在 `app/build/outputs/apk/debug/` 中找到 `app-debug.apk`

## 方法二：命令行构建

需要：JDK 17+, Android SDK

```bash
cd android
export ANDROID_HOME=/path/to/android-sdk
./gradlew assembleDebug
```

## 方法三：使用提供的预编译APK

如果没有构建环境，可以使用预编译的APK（如果提供）。

## 安装后设置

1. 安装 APK 到手机
2. 打开应用，点击"授权通知访问"
3. 在系统设置中找到 NotifySync 并开启权限
4. 返回应用，输入电脑IP（手机热点默认 192.168.43.1）
5. 点击保存
