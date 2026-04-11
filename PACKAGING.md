# 打包说明

## Windows EXE 打包步骤

### 1. 安装依赖

```bash
cd windows
pip install pyinstaller pystray pillow win11toast
```

### 2. 打包命令

```bash
pyinstaller --name=NotifySync \
    --onefile \
    --windowed \
    --hidden-import=win11toast \
    --hidden-import=pystray \
    --hidden-import=PIL \
    --hidden-import=winreg \
    --clean \
    notify_server_tray.py
```

### 3. 输出位置

打包后的 EXE 在 `dist/NotifySync.exe`

### 4. 分发

将 `NotifySync.exe` 单独复制到任意位置即可运行，无需其他文件。

---

## 安卓 APK 构建步骤

### 1. 打开项目

使用 Android Studio 打开 `android/` 文件夹

### 2. 同步项目

点击 "Sync Project with Gradle Files"

### 3. 构建 APK

Build → Build Bundle(s) / APK(s) → Build APK(s)

### 4. 获取 APK

在 `app/build/outputs/apk/debug/app-debug.apk`

### 5. 签名（可选）

如需发布到应用商店，需要生成签名 APK：
Build → Generate Signed Bundle / APK
