<div align="center">

<img src="assets/logo.png" width="120" alt="NotifySync Logo">

# NotifySync

**将安卓手机通知实时同步到 Windows 通知栏**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/Platform-Android%20%7C%20Windows-brightgreen.svg)](https://github.com/xiabiqing/NotifySync)
[![Release](https://img.shields.io/badge/Release-v2.0-orange.svg)](https://github.com/xiabiqing/NotifySync/releases)

📱 **实时同步** · 🔒 **安全可靠** · 🖥️ **原生体验** · ⚙️ **开机自启**

[下载 Windows 版](https://github.com/xiabiqing/NotifySync/releases/latest/download/NotifySync.exe) · [下载安卓版](https://github.com/xiabiqing/NotifySync/releases/latest/download/app-debug.apk) · [使用说明](docs/USAGE.md)

</div>

---

## ✨ 功能特性

| 功能 | 描述 |
|------|------|
| 📱 **实时同步** | 手机收到通知瞬间，电脑立即显示，延迟极低 |
| 🖥️ **原生体验** | 融入 Windows 11 通知中心，支持隐私模式（应用内提醒）|
| 🔒 **安全可靠** | 支持 Token 鉴权和 AES 加密，仅在局域网传输 |
| ⚙️ **开机自启** | 支持系统托盘后台运行，静默无打扰 |
| 🎨 **现代 UI** | Material Design 风格界面，简洁美观 |
| 📊 **状态监控** | 实时显示连接状态、授权状态 |

## 🚀 快速开始

### 下载安装

| 平台 | 下载 | 大小 |
|------|------|------|
| Windows | [NotifySync.exe](https://github.com/xiabiqing/NotifySync/releases/latest/download/NotifySync.exe) | ~24 MB |
| Android | [app-debug.apk](https://github.com/xiabiqing/NotifySync/releases/latest/download/app-debug.apk) | ~1.7 MB |

### 三步配置

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   1. 启动电脑端  │ ──▶ │   2. 配置手机端  │ ──▶ │   3. 开始同步   │
│   运行 EXE 文件  │     │  输入电脑 IP:端口 │     │  测试并确认连通  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**详细步骤：**

1. **电脑端**：下载并运行 `NotifySync.exe`，记录界面显示的 IP 地址和端口
2. **手机端**：安装 APK，授予通知监听权限，填写电脑 IP 地址和端口
3. **测试连接**：发送测试通知，确认连通后即可正常使用

> 💡 **提示**：确保手机和电脑连接在同一 Wi-Fi 网络下

## 📸 界面预览

<div align="center">

| Windows 服务端 | Android 客户端 |
|:------------:|:------------:|
| ![Windows UI](assets/screenshot-windows.png) | ![Android UI](assets/screenshot-android.png) |

</div>

## 🛠️ 技术栈

### Windows 端
- Python 3.8+
- tkinter (GUI)
- pystray (系统托盘)
- win11toast (Windows 11 原生通知)
- PyInstaller (打包 EXE)

### Android 端
- Java / Kotlin
- Android SDK 34
- NotificationListenerService (系统通知监听)
- Material Design 3 (UI)

## 📡 通信协议

安卓端通过 HTTP POST 发送通知数据到 Windows：

```json
{
  "id": "notification_key",
  "appName": "微信",
  "packageName": "com.tencent.mm",
  "title": "张三",
  "text": "你好，在吗？",
  "subText": "",
  "time": 1712654321000
}
```

## 🔧 自行构建

### Windows EXE 打包

```bash
cd windows
pip install pyinstaller pystray pillow win11toast
python build_exe.py
```

### Android APK 构建

1. 用 Android Studio 打开 `android/` 文件夹
2. 同步 Gradle（Sync Project with Gradle Files）
3. Build → Build Bundle(s) / APK(s) → Build APK(s)
4. 在 `app/build/outputs/apk/debug/` 中找到 APK

详细构建说明见 [BUILD_GUIDE.md](BUILD_GUIDE.md)

## 📝 使用场景

- 💼 **工作沟通** - Boss直聘、企业微信等消息实时提醒
- 🛒 **二手交易** - 闲鱼、转转等平台消息不漏接
- 🔐 **验证码** - 手机验证码即时显示在电脑
- 📢 **系统提醒** - 来电、短信、日程提醒同步显示

## ⚠️ 注意事项

- 本软件为开源工具，按「现状」提供
- 建议仅在可信局域网中使用，不建议将端口暴露到公网
- 使用前请配置安全 Token 和加密密钥
- 详细免责声明见 [使用说明与免责声明.md](使用说明与免责声明.md)

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建你的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

## 📄 开源协议

本项目采用 [MIT](LICENSE) 协议开源。

## 👤 作者

- **尼古拉.小侠碧青**
- GitHub: [@xiabiqing](https://github.com/xiabiqing)
- 微信: xiabiqing1
- QQ: 2632493933

---

<div align="center">

如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！

</div>
