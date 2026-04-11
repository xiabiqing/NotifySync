#!/usr/bin/env python3
"""
NotifySync Windows Server - 系统托盘版
支持：系统托盘图标、开机自启动、最小化到托盘
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import re
import sys
import threading
import time
import shutil
import winsound
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser

try:
    from Crypto.Cipher import AES
except Exception:
    try:
        from Cryptodome.Cipher import AES
    except Exception:
        AES = None

# Windows 通知库
try:
    from win11toast import notify
    TOAST_BACKEND = "win11toast"
except ImportError:
    try:
        from win10toast import ToastNotifier
        TOAST_BACKEND = "win10toast"
        _toaster = ToastNotifier()
    except ImportError:
        try:
            from plyer import notification
            TOAST_BACKEND = "plyer"
        except ImportError:
            TOAST_BACKEND = None

# 系统托盘
try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('NotifySync')

# Windows API for setting taskbar icon
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:
    user32 = None
    kernel32 = None


def get_windows_work_area():
    """获取不含任务栏的工作区坐标 (left, top, right, bottom)。"""
    if os.name != 'nt' or not user32:
        return None
    try:
        rect = wintypes.RECT()
        ok = user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        if ok:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return None

HOST = "0.0.0.0"
PORT = 8787


def set_windows_app_id():
    """设置 Windows AppUserModelID，确保任务栏首次显示正确图标"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('NotifySync.App')
    except Exception as e:
        logger.warning(f"设置 AppUserModelID 失败: {e}")


def enable_windows_dpi_awareness():
    """启用高DPI感知，避免字体发虚"""
    if os.name != 'nt':
        return
    try:
        # Windows 10/11 推荐：Per Monitor V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        # Windows 8.1 兜底
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        # 旧系统兜底
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        logger.warning(f"设置DPI感知失败: {e}")


def get_resource_path(filename: str):
    """获取资源文件路径（兼容 PyInstaller onefile）"""
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, filename)


def get_icon_candidates(ext: str):
    """收集可能的图标路径（优先根目录固定名，再到 icon 子目录）"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []

    # 1) 固定文件名（兼容现有打包逻辑）
    if ext == 'ico':
        names = ['notifysync.ico', 'app.ico', 'icon.ico', 'logo.ico']
    else:
        names = ['notifysync.png', 'app.png', 'icon.png', 'logo.png']

    for name in names:
        candidates.append(get_resource_path(name))
        candidates.append(os.path.join(base_dir, name))

    # 2) icon 子目录递归扫描（你现在上传的目录结构）
    icon_root = os.path.join(base_dir, 'icon')
    if os.path.isdir(icon_root):
        found = []
        for root, _, files in os.walk(icon_root):
            for f in files:
                if f.lower().endswith(f'.{ext}'):
                    found.append(os.path.join(root, f))
        # 按修改时间降序，优先最新替换的图标
        found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        candidates.extend(found)

    # 去重且保序
    uniq = []
    seen = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def get_config_path():
    appdata = os.getenv('APPDATA')
    if appdata:
        return os.path.join(appdata, 'NotifySync', 'config.json')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notifysync_config.json')


def load_app_config():
    path = get_config_path()
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"读取配置失败: {e}")
    return {}


def save_app_config(config: dict):
    path = get_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return False


def _derive_keys(secret: str):
    secret_bytes = secret.encode('utf-8')
    aes_key = hashlib.sha256(b'NS-AES|' + secret_bytes).digest()
    hmac_key = hashlib.sha256(b'NS-HMAC|' + secret_bytes).digest()
    return aes_key, hmac_key


def decrypt_encrypted_payload(data: dict, secret: str):
    """解密手机端加密负载，返回通知 dict"""
    if not isinstance(data, dict) or data.get('enc') != 'v1':
        return data

    if not secret:
        raise ValueError('缺少加密密钥')

    ts = int(data.get('ts', 0))
    now = int(time.time())
    # 允许更大的时钟偏差，避免手机/电脑时间不一致导致全部失败
    if abs(now - ts) > 1800:
        raise ValueError('请求时间偏差过大')

    nonce = str(data.get('nonce', ''))
    iv_b64 = str(data.get('iv', ''))
    cipher_b64 = str(data.get('data', ''))
    sig_b64 = str(data.get('sig', ''))

    if not (nonce and iv_b64 and cipher_b64 and sig_b64):
        raise ValueError('加密字段不完整')

    aes_key, hmac_key = _derive_keys(secret)
    to_sign = f"{ts}.{nonce}.{iv_b64}.{cipher_b64}".encode('utf-8')
    expected_sig = base64.b64encode(hmac.new(hmac_key, to_sign, hashlib.sha256).digest()).decode('utf-8')

    if not hmac.compare_digest(expected_sig, sig_b64):
        raise ValueError('签名校验失败')

    if AES is None:
        raise ValueError('缺少解密依赖 pycryptodome')

    iv = base64.b64decode(iv_b64)
    encrypted = base64.b64decode(cipher_b64)
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
    plain = cipher.decrypt_and_verify(encrypted[:-16], encrypted[-16:])
    return json.loads(plain.decode('utf-8'))


class NotifySyncServer:
    def __init__(self, host: str = HOST, port: int = PORT, auth_token: str = "", crypto_key: str = ""):
        self.host = host
        self.port = port
        self.auth_token = auth_token.strip() if auth_token else ""
        self.crypto_key = crypto_key.strip() if crypto_key else ""
        self.httpd = None
        self.thread = None
        self.running = False
        self.log_callback = None

        # 智能静默防重配置
        self.smart_mute_wechat = True
        self.smart_mute_qq = True
        self.smart_mute_wecom = True
        self.smart_mute_tim = True
        self.smart_mute_dingtalk = True
        self.app_mute_overrides = {}  # {package_name: bool}
        self.app_process_map = {
            'com.tencent.mm': 'WeChat.exe',
            'com.tencent.mobileqq': 'QQ.exe',
            'com.tencent.wework': 'WXWork.exe',
            'com.tencent.tim': 'TIM.exe',
            'com.alibaba.android.rimet': 'DingTalk.exe',
        }
        self.on_app_seen = None
        self.on_notification = None

    def log(self, message: str):
        logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def start(self):
        if self.running:
            return

        handler_cls = self._build_handler()
        self.httpd = HTTPServer((self.host, self.port), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.log(f"服务已启动: http://0.0.0.0:{self.port}")

    def stop(self):
        if not self.running:
            return

        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        self.running = False
        self.log("服务已停止")

    def should_mute_notification(self, package_name: str):
        pkg = _normalize_pkg_name(package_name)
        if not pkg:
            return False, ''

        # 用户手动覆盖优先
        if pkg in self.app_mute_overrides:
            if not self.app_mute_overrides[pkg]:
                return False, '用户设置为提醒'
            proc = self.app_process_map.get(pkg, '')
            if not proc:
                return True, '用户设置为静默'
            proc_candidates = _alias_process_names(proc)
            if is_windows_process_running(proc_candidates):
                return True, f"PC客户端在线: {'/'.join(proc_candidates)}"
            return False, f"未检测到客户端进程: {'/'.join(proc_candidates)}"

        # 默认推荐规则
        rules = {}
        if self.smart_mute_wechat:
            rules['com.tencent.mm'] = 'WeChat.exe'
        if self.smart_mute_qq:
            rules['com.tencent.mobileqq'] = 'QQ.exe'
        if self.smart_mute_wecom:
            rules['com.tencent.wework'] = 'WXWork.exe'
        if self.smart_mute_tim:
            rules['com.tencent.tim'] = 'TIM.exe'
        if self.smart_mute_dingtalk:
            rules['com.alibaba.android.rimet'] = 'DingTalk.exe'

        proc = rules.get(pkg)
        if not proc:
            return False, '未配置静默'

        proc_candidates = _alias_process_names(proc)
        if is_windows_process_running(proc_candidates):
            return True, f"PC客户端在线: {'/'.join(proc_candidates)}"

        self.log(f"未命中静默进程: pkg={pkg}, expected={'/'.join(proc_candidates)}")
        return False, f"未检测到客户端进程: {'/'.join(proc_candidates)}"

    def _build_handler(self):
        outer = self

        class NotificationHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                # 兼容 /notify、/notify/、/notify?x=1 等路径
                request_path = self.path.split('?', 1)[0].rstrip('/')

                # 先记录请求到达（便于排查“首启没有任何日志”）
                auth_header = self.headers.get('Authorization', '')
                x_token = self.headers.get('X-NotifySync-Token', '')
                has_bearer = auth_header.startswith('Bearer ')
                outer.log(
                    f"收到POST: path={self.path}, from={self.client_address[0]}, "
                    f"has_bearer={has_bearer}, has_x_token={bool(x_token)}"
                )

                if request_path != '/notify':
                    outer.log(f"忽略非 /notify 请求: {self.path}")
                    self.send_error(404)
                    return

                try:
                    expected_token = outer.auth_token
                    if expected_token:
                        bearer = auth_header[7:].strip() if has_bearer else ''
                        if (not bearer or bearer != expected_token) and (not x_token or x_token != expected_token):
                            outer.log("拒绝未授权请求: token不匹配")
                            self.send_response(401)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({'status': 'unauthorized'}).encode())
                            return

                    content_length = int(self.headers.get('Content-Length', 0))
                    post_data = self.rfile.read(content_length)
                    raw_text = post_data.decode('utf-8', errors='ignore').strip()
                    data = json.loads(raw_text or '{}')

                    if isinstance(data, dict) and data.get('enc') == 'v1':
                        outer.log('收到加密消息，开始解密')
                        if not outer.crypto_key:
                            raise ValueError('收到加密消息，但电脑端未配置加密密钥')
                        data = decrypt_encrypted_payload(data, outer.crypto_key)
                        outer.log('加密消息解密成功')
                    elif outer.crypto_key:
                        raise ValueError('已设置加密密钥，拒绝未加密请求')

                    # 保持原字段优先，同时兼容常见别名字段
                    app_name = data.get('appName') or data.get('app_name') or '未知应用'
                    title = data.get('title') or data.get('notificationTitle') or ''
                    text = data.get('text') or data.get('content') or data.get('message') or data.get('body') or ''
                    sub_text = data.get('subText') or data.get('sub_text') or data.get('summary') or ''
                    package_name = data.get('packageName') or data.get('package_name') or ''

                    if not isinstance(app_name, str):
                        app_name = str(app_name)
                    if not isinstance(title, str):
                        title = str(title)
                    if not isinstance(text, str):
                        text = str(text)
                    if not isinstance(sub_text, str):
                        sub_text = str(sub_text)

                    if callable(outer.on_app_seen):
                        try:
                            outer.on_app_seen(app_name, package_name)
                        except Exception:
                            pass

                    if not (title or '').strip() and not (text or '').strip() and not (sub_text or '').strip():
                        outer.log(f"忽略空通知: {app_name} | {package_name}")
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({'status': 'ok', 'ignored': True, 'reason': 'empty_payload'}).encode())
                        return

                    mute, reason = outer.should_mute_notification(package_name)
                    if mute:
                        outer.log(f"已静默（{reason}）: {app_name} | {title or '(无标题)'} | {package_name}")
                    else:
                        handled = False
                        if callable(outer.on_notification):
                            try:
                                handled = bool(outer.on_notification(app_name, title, text, sub_text, package_name))
                            except Exception as ex:
                                outer.log(f"通知回调失败: {ex}")
                        if not handled:
                            show_notification(app_name, title, text, sub_text)
                        outer.log(f"收到通知: {app_name} | {title or '(无标题)'} | {package_name}")

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'ok', 'muted': mute, 'reason': reason}).encode())

                except json.JSONDecodeError as e:
                    outer.log(f"JSON 解析错误: {e}")
                    self.send_error(400, "Invalid JSON")
                except Exception as e:
                    outer.log(f"处理请求错误: {e.__class__.__name__}: {e}")
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'error', 'message': f'{e.__class__.__name__}: {e}'}).encode())

            def do_GET(self):
                if self.path == '/health':
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'status': 'running',
                        'backend': TOAST_BACKEND or 'console',
                        'timestamp': datetime.now().isoformat()
                    }).encode())
                elif self.path == '/test':
                    show_notification("NotifySync", "测试通知", "如果你看到了这条通知，说明电脑端显示正常")
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'ok'}).encode())
                    outer.log("已触发电脑端测试通知")
                else:
                    self.send_error(404)

        return NotificationHandler


def parse_notification_payload(data: dict):
    """兼容不同手机端上报字段，提取通知内容"""
    if not isinstance(data, dict):
        return "未知应用", "", "", "", ""

    def pick(*keys, default=""):
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text:
                return text
        return default

    app_name = pick(
        'appName', 'app_name', 'app', 'sourceApp', 'source_app',
        'packageLabel', 'package_label', default='未知应用'
    )
    title = pick('title', 'notificationTitle', 'notification_title', 'ticker')
    text = pick(
        'text', 'content', 'message', 'body', 'notificationText',
        'notification_text', 'bigText', 'big_text', 'android_text'
    )
    sub_text = pick('subText', 'sub_text', 'summary', 'summaryText', 'summary_text')
    package_name = pick('packageName', 'package_name', 'pkg', 'package')

    # 部分客户端把通知放在嵌套结构里
    if not (title or text or sub_text):
        nested = data.get('notification') or data.get('payload') or data.get('data')
        if isinstance(nested, dict):
            app_name2, title2, text2, sub_text2, package_name2 = parse_notification_payload(nested)
            if app_name == '未知应用' and app_name2:
                app_name = app_name2
            title = title or title2
            text = text or text2
            sub_text = sub_text or sub_text2
            package_name = package_name or package_name2

    if app_name == '未知应用' and package_name:
        app_name = package_name

    return app_name, title, text, sub_text, package_name


def show_notification(app_name: str, title: str, text: str, sub_text: str = ""):
    if sub_text and sub_text != text:
        body = f"{text}\n{sub_text}"
    else:
        body = text

    if not body.strip():
        body = "(无内容)"

    if len(body) > 256:
        body = body[:253] + "..."

    final_title = f"{app_name}: {title}" if title else app_name

    try:
        if TOAST_BACKEND == "win11toast":
            notify(title=final_title, body=body, app_id="NotifySync", duration="short")
        elif TOAST_BACKEND == "win10toast":
            _toaster.show_toast(title=final_title, msg=body, duration=5, threaded=True)
        elif TOAST_BACKEND == "plyer":
            notification.notify(title=final_title, message=body, timeout=5)
        else:
            logger.info(f"[{app_name}] {title}: {body}")
    except Exception as e:
        logger.error(f"显示通知失败: {e}")


def parse_ipconfig_adapters(output: str):
    """解析 ipconfig 输出，提取网卡名称、IPv4、网关等信息"""
    adapters = []
    current = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.endswith(':') and ('适配器' in line or 'adapter' in line.lower()):
            if current:
                adapters.append(current)
            current = {
                'name': line.rstrip(':'),
                'ipv4': None,
                'gateway': None,
            }
            continue

        if not current:
            continue

        ipv4_match = re.search(r'IPv4[^\d]*(\d{1,3}(?:\.\d{1,3}){3})', line, re.IGNORECASE)
        if ipv4_match:
            current['ipv4'] = ipv4_match.group(1)

        gateway_match = re.search(r'默认网关[^\d]*(\d{1,3}(?:\.\d{1,3}){3})', line, re.IGNORECASE)
        if gateway_match:
            current['gateway'] = gateway_match.group(1)

    if current:
        adapters.append(current)

    return adapters


def get_ip_addresses():
    """使用 ipconfig 获取可用 IPv4，并尽量过滤虚拟网卡"""
    ips = []
    try:
        result = subprocess.run(['ipconfig'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        adapters = parse_ipconfig_adapters(result.stdout)

        virtual_keywords = [
            'virtual', 'vmware', 'vbox', 'hyper-v', 'host-only', 'loopback',
            'bluetooth', 'docker', 'wsl', 'teredo', 'isatap', 'wi-fi direct',
            'vEthernet', '虚拟', '回环', '蓝牙'
        ]

        def adapter_score(item):
            name = (item.get('name') or '').lower()
            ip = item.get('ipv4')
            gateway = item.get('gateway')
            if not ip or ip.startswith('127.'):
                return -999

            score = 0
            if gateway:
                score += 40
            if ('wlan' in name) or ('wi-fi' in name) or ('wireless' in name) or ('无线' in name):
                score += 35
            if 'ethernet' in name or '以太网' in name:
                score += 25
            if any(k in name for k in virtual_keywords):
                score -= 80
            if ip.startswith('10.'):
                score += 20
            elif ip.startswith('192.168.'):
                score += 10
            elif ip.startswith('172.'):
                score += 5
            return score

        ranked = sorted(adapters, key=adapter_score, reverse=True)
        for item in ranked:
            ip = item.get('ipv4')
            if ip and not ip.startswith('127.') and ip not in ips:
                ips.append(ip)

    except Exception as e:
        logger.warning(f"ipconfig 获取 IP 失败: {e}")

    if not ips:
        try:
            hostname = socket.gethostname()
            addresses = socket.getaddrinfo(hostname, None)
            for addr in addresses:
                ip = addr[4][0]
                if ':' in ip or ip.startswith('127.'):
                    continue
                if ip not in ips:
                    ips.append(ip)
        except Exception as e2:
            logger.error(f"备用方式也失败: {e2}")

    return ips


def choose_recommended_ip(ips):
    """推荐优先级：10.x.x.x > 192.168.x.x > 172.16-31.x.x > 其他"""
    if not ips:
        return None

    preferred_prefixes = [
        '10.', '192.168.',
        '172.16.', '172.17.', '172.18.', '172.19.',
        '172.20.', '172.21.', '172.22.', '172.23.',
        '172.24.', '172.25.', '172.26.', '172.27.',
        '172.28.', '172.29.', '172.30.', '172.31.'
    ]

    for prefix in preferred_prefixes:
        for ip in ips:
            if ip.startswith(prefix):
                return ip
    return ips[0]


def _normalize_pkg_name(name: str) -> str:
    return (name or '').strip().lower()


def _normalize_process_name(name: str) -> str:
    value = (name or '').strip()
    if not value:
        return ''
    return value if value.lower().endswith('.exe') else f"{value}.exe"


def _alias_process_names(name: str):
    """常见客户端进程别名，解决不同版本/安装渠道差异。"""
    normalized = _normalize_process_name(name)
    if not normalized:
        return []

    aliases = {
        # 微信不同版本常见进程名：WeChat.exe / WeChatApp.exe / WeChatAppEx.exe
        'wechat.exe': ['wechat.exe', 'wechatapp.exe', 'wechatappex.exe'],
        # 兼容用户把规则写成 WeChatAppEx
        'wechatappex.exe': ['wechatappex.exe', 'wechatapp.exe', 'wechat.exe'],
        'qq.exe': ['qq.exe'],
        'wxwork.exe': ['wxwork.exe', 'wecom.exe'],
        'tim.exe': ['tim.exe'],
        'dingtalk.exe': ['dingtalk.exe'],
    }
    return aliases.get(normalized.lower(), [normalized])


def is_windows_process_running(process_names):
    """检测指定进程是否存在（仅 Windows）。"""
    if os.name != 'nt':
        return False

    wanted = {(_normalize_process_name(p)).lower() for p in process_names if _normalize_process_name(p)}
    if not wanted:
        return False

    try:
        creationflags = 0
        if os.name == 'nt':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

        result = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            creationflags=creationflags
        )
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            # CSV 第一列是进程名，例如 "WeChat.exe"
            if line.startswith('"') and '","' in line:
                proc = line.split('","', 1)[0].strip('"').lower()
            else:
                proc = line.split(',', 1)[0].strip().strip('"').lower()
            if proc in wanted:
                return True
    except Exception as e:
        logger.warning(f"检测进程失败: {e}")

    return False


# ========== 系统托盘功能 ==========

def create_tray_icon():
    """创建系统托盘图标（优先使用 png，其次 ico；支持 icon 子目录）"""
    candidates = get_icon_candidates('png') + get_icon_candidates('ico')

    for icon_path in candidates:
        if os.path.exists(icon_path):
            try:
                img = Image.open(icon_path).convert('RGBA')
                # 托盘图标通常使用64x64效果最佳
                if img.size != (64, 64):
                    img = img.resize((64, 64), Image.LANCZOS)
                logger.info(f"托盘图标已加载: {icon_path}")
                return img
            except Exception as e:
                logger.warning(f"加载托盘图标失败 {icon_path}: {e}")

    # 备选：绘制默认图标
    width = 64
    height = 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)

    # 画一个蓝色圆形背景
    dc.ellipse([4, 4, width-4, height-4], fill='#2196F3')
    # 画白色铃铛图标（简化版）
    dc.ellipse([width//2-6, 12, width//2+6, 24], fill='white')  # 铃铛顶部
    dc.polygon([(width//2-12, 24), (width//2+12, 24), (width//2+16, 48), (width//2-16, 48)], fill='white')  # 铃铛主体

    return image


AUTOSTART_VALUE_NAME = 'NotifySync'


def _build_autostart_command():
    """构建安全的自启动命令（不走 cmd）"""
    if getattr(sys, 'frozen', False):
        exe = os.path.abspath(sys.executable)
        if os.path.isfile(exe) and exe.lower().endswith('.exe'):
            return f'"{exe}"'
        return None

    # 开发模式：优先 pythonw.exe，避免黑窗和策略拦截 cmd
    py = os.path.abspath(sys.executable)
    pyw = py[:-4] + 'w.exe' if py.lower().endswith('.exe') else py
    python_bin = pyw if os.path.isfile(pyw) else py
    script = os.path.abspath(__file__)
    if not (os.path.isfile(python_bin) and os.path.isfile(script)):
        return None
    return f'"{python_bin}" "{script}"'


def is_autostart_enabled():
    """检查是否已设置开机自启动"""
    try:
        import winreg
        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
                return bool((value or '').strip())
            except FileNotFoundError:
                return False
    except Exception:
        return False


def set_autostart(enabled: bool):
    """设置开机自启动"""
    try:
        import winreg
        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = _build_autostart_command()
                if not cmd:
                    logger.error('无法构建安全的自启动命令')
                    return False
                winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, cmd)
                logger.info(f"已设置开机自启动: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                    logger.info("已取消开机自启动")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        logger.error(f"设置自启动失败: {e}")
        return False


# ========== GUI 界面 ==========

class NotifySyncGUI:
    def __init__(self):
        enable_windows_dpi_awareness()
        set_windows_app_id()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("NotifySync - 手机通知同步到电脑")
        self.root.geometry("980x800")
        self.root.minsize(900, 700)
        self.root.resizable(True, True)
        self._tune_tk_scaling_for_dpi()
        self._center_main_window()
        self._apply_window_icon()

        # 设置主题色
        self.colors = {
            'primary': '#0F6CBD',
            'success': '#107C10',
            'warning': '#986F0B',
            'bg': '#F5F5F5',
            'card': '#FFFFFF',
            'text': '#1B1A19',
            'muted': '#605E5C',
            'border': '#E1DFDD'
        }

        self.config = load_app_config()
        self.auth_token = str(self.config.get('auth_token', '')).strip()
        self.crypto_key = str(self.config.get('crypto_key', '')).strip()

        # 智能静默防重配置
        self.smart_mute_wechat = bool(self.config.get('smart_mute_wechat', True))
        self.smart_mute_qq = bool(self.config.get('smart_mute_qq', True))
        self.smart_mute_wecom = bool(self.config.get('smart_mute_wecom', True))
        self.smart_mute_tim = bool(self.config.get('smart_mute_tim', True))
        self.smart_mute_dingtalk = bool(self.config.get('smart_mute_dingtalk', True))
        self.app_mute_overrides = dict(self.config.get('app_mute_overrides', {}) or {})
        self.app_process_map = {
            'com.tencent.mm': 'WeChat.exe',
            'com.tencent.mobileqq': 'QQ.exe',
            'com.tencent.wework': 'WXWork.exe',
            'com.tencent.tim': 'TIM.exe',
            'com.alibaba.android.rimet': 'DingTalk.exe',
        }
        saved_map = dict(self.config.get('app_process_map', {}) or {})
        for k, v in saved_map.items():
            kk = _normalize_pkg_name(k)
            vv = _normalize_process_name(v)
            if kk and vv:
                self.app_process_map[kk] = vv

        self.known_apps = dict(self.config.get('known_apps', {}) or {})
        self.privacy_mode = bool(self.config.get('privacy_mode', False))
        self.popup_duration_ms = int(self.config.get('popup_duration_ms', 4500) or 4500)
        self.popup_sound = bool(self.config.get('popup_sound', False))
        default_store = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'NotifySync', 'privacy_notifications')
        self.local_store_dir = str(self.config.get('local_store_dir', default_store)).strip() or default_store

        self.server = NotifySyncServer(port=PORT, auth_token=self.auth_token, crypto_key=self.crypto_key)
        self.server.log_callback = self.add_log
        self.server.on_app_seen = self._on_app_seen
        self.server.on_notification = self._handle_notification_delivery
        self._apply_smart_mute_to_server()

        self.tray_icon = None
        self.tray_thread = None

        self._build_ui()
        # UI构建后再次居中，避免pack后尺寸变化导致偏移
        self._center_main_window()
        # 延迟再设置一次窗口图标，避免首次启动时任务栏短暂显示默认图标
        self.root.after(120, self._apply_window_icon)
        self.root.deiconify()
        self._show_network_info()
        self._start_server()

        # 关闭时最小化到托盘
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _tune_tk_scaling_for_dpi(self):
        """根据系统DPI微调Tk缩放，提升文字清晰度"""
        if os.name != 'nt':
            return
        try:
            dpi = self.root.winfo_fpixels('1i')
            if dpi and dpi > 0:
                scale = dpi / 72.0
                self.root.tk.call('tk', 'scaling', scale)
        except Exception as e:
            logger.warning(f"设置Tk缩放失败: {e}")

    def _center_main_window(self):
        """启动时窗口居中（工作区内，避免压任务栏）"""
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width() or 980
            h = self.root.winfo_height() or 800

            work = get_windows_work_area()
            if work:
                left, top, right, bottom = work
                area_w = max(1, right - left)
                area_h = max(1, bottom - top)
                x = left + max(0, (area_w - w) // 2)
                y = top + max(0, (area_h - h) // 2) - int(area_h * 0.03)
                y = max(top, y)
            else:
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)

            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception as e:
            logger.warning(f"窗口居中失败: {e}")

    def _apply_smart_mute_to_server(self):
        self.server.smart_mute_wechat = bool(self.smart_mute_wechat)
        self.server.smart_mute_qq = bool(self.smart_mute_qq)
        self.server.smart_mute_wecom = bool(self.smart_mute_wecom)
        self.server.smart_mute_tim = bool(self.smart_mute_tim)
        self.server.smart_mute_dingtalk = bool(self.smart_mute_dingtalk)
        self.server.app_mute_overrides = {
            _normalize_pkg_name(k): bool(v) for k, v in self.app_mute_overrides.items() if _normalize_pkg_name(k)
        }
        self.server.app_process_map = dict(self.app_process_map)

    def _save_smart_mute_config(self):
        self.config['smart_mute_wechat'] = bool(self.smart_mute_wechat)
        self.config['smart_mute_qq'] = bool(self.smart_mute_qq)
        self.config['smart_mute_wecom'] = bool(self.smart_mute_wecom)
        self.config['smart_mute_tim'] = bool(self.smart_mute_tim)
        self.config['smart_mute_dingtalk'] = bool(self.smart_mute_dingtalk)
        self.config['app_mute_overrides'] = dict(self.app_mute_overrides)
        self.config['app_process_map'] = dict(self.app_process_map)
        self.config['known_apps'] = dict(self.known_apps)
        ok = save_app_config(self.config)
        if ok:
            self._apply_smart_mute_to_server()
        return ok

    def _btn_primary(self, parent, text, command, danger=False):
        bg = '#D13438' if danger else self.colors['primary']
        active = '#A4262C' if danger else '#115EA3'
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg='white',
            activebackground=active,
            activeforeground='white',
            font=('Segoe UI', 10),
            relief=tk.FLAT,
            cursor='hand2',
            padx=12,
            pady=4,
            bd=0,
            highlightthickness=0
        )

    def _btn_subtle(self, parent, text, command):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg='#F3F2F1',
            fg=self.colors['text'],
            activebackground='#EDEBE9',
            activeforeground=self.colors['text'],
            font=('Segoe UI', 9),
            relief=tk.FLAT,
            cursor='hand2',
            padx=12,
            pady=4,
            bd=0,
            highlightthickness=0
        )

    def _build_ui(self):
        """构建现代化UI"""
        app_container = tk.Frame(self.root, bg=self.colors['bg'])
        app_container.pack(fill=tk.BOTH, expand=True)

        # 左侧导航
        nav_frame = tk.Frame(app_container, bg='#FAFAFA', width=196,
                             highlightbackground=self.colors['border'], highlightthickness=1)
        nav_frame.pack(side=tk.LEFT, fill=tk.Y)
        nav_frame.pack_propagate(False)

        nav_title = tk.Label(
            nav_frame,
            text='NotifySync',
            font=('Segoe UI', 14, 'bold'),
            fg=self.colors['text'],
            bg='#FAFAFA'
        )
        nav_title.pack(anchor=tk.W, padx=16, pady=(18, 8))

        nav_subtitle = tk.Label(
            nav_frame,
            text='桌面控制台',
            font=('Segoe UI', 9),
            fg=self.colors['muted'],
            bg='#FAFAFA'
        )
        nav_subtitle.pack(anchor=tk.W, padx=16, pady=(0, 16))

        self.nav_btn_basic = tk.Button(
            nav_frame,
            text='  基础设置',
            command=lambda: self._show_left_page('basic'),
            bg='#E8F3FC', fg=self.colors['primary'],
            activebackground='#DCEEFE', activeforeground=self.colors['primary'],
            font=('Segoe UI', 10, 'bold'),
            relief=tk.FLAT, cursor='hand2',
            anchor='w', padx=14, pady=10,
            bd=0, highlightthickness=0
        )
        self.nav_btn_basic.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.nav_btn_logs = tk.Button(
            nav_frame,
            text='  日志输出',
            command=lambda: self._show_left_page('logs'),
            bg='#FAFAFA', fg=self.colors['text'],
            activebackground='#F3F2F1', activeforeground=self.colors['text'],
            font=('Segoe UI', 10),
            relief=tk.FLAT, cursor='hand2',
            anchor='w', padx=14, pady=10,
            bd=0, highlightthickness=0
        )
        self.nav_btn_logs.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.nav_btn_mode = tk.Button(
            nav_frame,
            text='  模式选择（必看）',
            command=lambda: self._show_left_page('mode'),
            bg='#FAFAFA', fg=self.colors['text'],
            activebackground='#F3F2F1', activeforeground=self.colors['text'],
            font=('Segoe UI', 10),
            relief=tk.FLAT, cursor='hand2',
            anchor='w', padx=14, pady=10,
            bd=0, highlightthickness=0
        )
        self.nav_btn_mode.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.nav_btn_security = tk.Button(
            nav_frame,
            text='  安全配置（推荐）',
            command=lambda: self._show_left_page('security'),
            bg='#FAFAFA', fg=self.colors['text'],
            activebackground='#F3F2F1', activeforeground=self.colors['text'],
            font=('Segoe UI', 10),
            relief=tk.FLAT, cursor='hand2',
            anchor='w', padx=14, pady=10,
            bd=0, highlightthickness=0
        )
        self.nav_btn_security.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.nav_btn_author = tk.Button(
            nav_frame,
            text='  作者信息',
            command=lambda: self._show_left_page('author'),
            bg='#FAFAFA', fg=self.colors['text'],
            activebackground='#F3F2F1', activeforeground=self.colors['text'],
            font=('Segoe UI', 10),
            relief=tk.FLAT, cursor='hand2',
            anchor='w', padx=14, pady=10,
            bd=0, highlightthickness=0
        )
        self.nav_btn_author.pack(fill=tk.X, padx=10)

        # 右侧内容区
        right_container = tk.Frame(app_container, bg=self.colors['bg'])
        right_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        header = ttk.Frame(right_container, padding=(20, 18, 20, 8))
        header.pack(fill=tk.X)

        title_label = tk.Label(header, text="NotifySync",
                               font=('Segoe UI', 24, 'bold'),
                               fg=self.colors['text'],
                               bg=self.colors['bg'])
        title_label.pack(side=tk.LEFT)

        subtitle = tk.Label(header, text="手机通知同步助手",
                           font=('Segoe UI', 10),
                           fg=self.colors['muted'],
                           bg=self.colors['bg'])
        subtitle.pack(side=tk.LEFT, padx=(10, 0), pady=(6, 0))

        self.content_basic = tk.Frame(right_container, bg=self.colors['bg'])
        self.content_logs = tk.Frame(right_container, bg=self.colors['bg'])
        self.content_author = tk.Frame(right_container, bg=self.colors['bg'])
        self.content_basic.pack(fill=tk.BOTH, expand=True, padx=20, pady=(6, 16))

        # 基础设置页滚动容器
        basic_scroll_wrap = tk.Frame(self.content_basic, bg=self.colors['bg'])
        basic_scroll_wrap.pack(fill=tk.BOTH, expand=True)

        self.basic_canvas = tk.Canvas(basic_scroll_wrap, bg=self.colors['bg'], highlightthickness=0)
        self.basic_scrollbar = tk.Scrollbar(basic_scroll_wrap, orient=tk.VERTICAL, command=self.basic_canvas.yview)
        self.basic_canvas.configure(yscrollcommand=self.basic_scrollbar.set)

        self.basic_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.basic_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.content_basic_inner = tk.Frame(self.basic_canvas, bg=self.colors['bg'])
        self.basic_canvas_window = self.basic_canvas.create_window((0, 0), window=self.content_basic_inner, anchor='nw')

        def _on_basic_inner_configure(event):
            self.basic_canvas.configure(scrollregion=self.basic_canvas.bbox('all'))

        def _on_basic_canvas_configure(event):
            self.basic_canvas.itemconfigure(self.basic_canvas_window, width=event.width)

        self.content_basic_inner.bind('<Configure>', _on_basic_inner_configure)
        self.basic_canvas.bind('<Configure>', _on_basic_canvas_configure)

        # 鼠标悬停在基础设置区任意位置即可滚动
        self._basic_scroll_active = False
        self.basic_canvas.bind('<Enter>', lambda e: self._set_basic_scroll_active(True))
        self.basic_canvas.bind('<Leave>', lambda e: self._set_basic_scroll_active(False))
        self.content_basic_inner.bind('<Enter>', lambda e: self._set_basic_scroll_active(True))
        self.content_basic_inner.bind('<Leave>', lambda e: self._set_basic_scroll_active(False))
        self.root.bind_all('<MouseWheel>', self._on_basic_mousewheel)

        # 状态卡片
        status_card = tk.Frame(self.content_basic_inner, bg=self.colors['card'],
                               highlightbackground=self.colors['border'],
                               highlightthickness=1)
        status_card.pack(fill=tk.X, pady=(0, 15), ipady=10)

        status_header = tk.Frame(status_card, bg=self.colors['card'])
        status_header.pack(fill=tk.X, padx=15, pady=(10, 5))

        self.status_dot = tk.Label(status_header, text="●",
                                   font=('Arial', 14),
                                   fg=self.colors['success'],
                                   bg=self.colors['card'])
        self.status_dot.pack(side=tk.LEFT)

        self.status_text = tk.Label(status_header, text="服务运行中",
                                    font=('Segoe UI', 12, 'bold'),
                                    fg=self.colors['success'],
                                    bg=self.colors['card'])
        self.status_text.pack(side=tk.LEFT, padx=(5, 0))

        self.toggle_btn = self._btn_primary(status_header, "停止服务", self._toggle_server, danger=True)
        self.toggle_btn.config(font=('Segoe UI', 10), padx=16, pady=5)
        self.toggle_btn.pack(side=tk.RIGHT)

        # IP信息区域
        ip_frame = tk.Frame(status_card, bg=self.colors['card'])
        ip_frame.pack(fill=tk.X, padx=15, pady=5)

        tk.Label(ip_frame, text="服务器地址:",
                font=('Segoe UI', 10),
                fg=self.colors['muted'], bg=self.colors['card']).pack(side=tk.LEFT)

        self.ip_label = tk.Label(ip_frame, text="获取中...",
                                font=('Segoe UI', 10, 'bold'),
                                fg=self.colors['primary'],
                                bg=self.colors['card'])
        self.ip_label.pack(side=tk.LEFT, padx=(10, 0))

        # 复制按钮
        copy_btn = self._btn_primary(ip_frame, "复制地址", self._copy_ip)
        copy_btn.pack(side=tk.LEFT, padx=(15, 0))

        # 测试按钮
        test_btn = self._btn_primary(ip_frame, "发送测试通知", self._send_test)
        test_btn.pack(side=tk.RIGHT)

        # 设置区域
        settings_card = tk.Frame(self.content_basic_inner, bg=self.colors['card'],
                                highlightbackground=self.colors['border'],
                                highlightthickness=1)
        settings_card.pack(fill=tk.X, pady=(0, 15), ipady=10)

        settings_header = tk.Label(settings_card, text="⚙️ 设置",
                                  font=('Segoe UI', 12, 'bold'),
                                  bg=self.colors['card'], fg='#333333')
        settings_header.pack(anchor=tk.W, padx=15, pady=(10, 10))

        # 自启动选项
        autostart_frame = tk.Frame(settings_card, bg=self.colors['card'])
        autostart_frame.pack(fill=tk.X, padx=15, pady=5)

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        autostart_cb = tk.Checkbutton(autostart_frame,
                                      text="开机自动启动 NotifySync",
                                      variable=self.autostart_var,
                                      command=self._on_autostart_changed,
                                      font=('Segoe UI', 10),
                                      bg=self.colors['card'],
                                      activebackground=self.colors['card'])
        autostart_cb.pack(side=tk.LEFT)

        # 最小化到托盘选项
        tray_frame = tk.Frame(settings_card, bg=self.colors['card'])
        tray_frame.pack(fill=tk.X, padx=15, pady=5)

        self.tray_var = tk.BooleanVar(value=True)
        tray_cb = tk.Checkbutton(tray_frame,
                                text="关闭窗口时最小化到系统托盘",
                                variable=self.tray_var,
                                font=('Segoe UI', 10),
                                bg=self.colors['card'],
                                activebackground=self.colors['card'])
        tray_cb.pack(side=tk.LEFT)

        # 兼容旧配置，UI 不再展示旧的“智能静默防重”复选框

        app_policy_frame = tk.Frame(settings_card, bg=self.colors['card'])
        app_policy_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(6, 8))

        tk.Label(
            app_policy_frame,
            text="应用静默策略（更直观）：开启=静默，关闭=正常提醒",
            font=('Segoe UI', 9), fg=self.colors['muted'], bg=self.colors['card']
        ).pack(anchor=tk.W)

        toolbar = tk.Frame(app_policy_frame, bg=self.colors['card'])
        toolbar.pack(fill=tk.X, pady=(6, 6))

        self.btn_policy_all_on = self._btn_subtle(toolbar, "全部设为静默", self._set_all_app_policies_on)
        self.btn_policy_all_on.pack(side=tk.LEFT)

        self.btn_policy_all_off = self._btn_subtle(toolbar, "全部设为提醒", self._set_all_app_policies_off)
        self.btn_policy_all_off.pack(side=tk.LEFT, padx=(6, 0))

        self.btn_policy_refresh = self._btn_subtle(toolbar, "刷新应用列表", self._refresh_app_policy_rows)
        self.btn_policy_refresh.pack(side=tk.LEFT, padx=(6, 0))

        self.app_policy_list_wrap = tk.Frame(app_policy_frame, bg=self.colors['card'])
        self.app_policy_list_wrap.pack(fill=tk.BOTH, expand=True)

        self.app_policy_canvas = tk.Canvas(self.app_policy_list_wrap, bg=self.colors['card'], highlightthickness=0, height=180)
        self.app_policy_scroll = tk.Scrollbar(self.app_policy_list_wrap, orient=tk.VERTICAL, command=self.app_policy_canvas.yview)
        self.app_policy_canvas.configure(yscrollcommand=self.app_policy_scroll.set)

        self.app_policy_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.app_policy_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.app_policy_inner = tk.Frame(self.app_policy_canvas, bg=self.colors['card'])
        self.app_policy_canvas_window = self.app_policy_canvas.create_window((0, 0), window=self.app_policy_inner, anchor='nw')

        self.app_policy_inner.bind('<Configure>', lambda e: self.app_policy_canvas.configure(scrollregion=self.app_policy_canvas.bbox('all')))
        self.app_policy_canvas.bind('<Configure>', lambda e: self.app_policy_canvas.itemconfigure(self.app_policy_canvas_window, width=e.width))

        self._refresh_app_policy_rows()

        self.content_mode = tk.Frame(right_container, bg=self.colors['bg'])
        mode_card_page = tk.Frame(self.content_mode, bg=self.colors['card'],
                                  highlightbackground=self.colors['border'],
                                  highlightthickness=1)
        mode_card_page.pack(fill=tk.X, pady=(0, 15), padx=20, ipady=10)

        mode_title = tk.Label(mode_card_page, text="🧭 模式选择（必看）",
                              font=('Segoe UI', 12, 'bold'),
                              fg=self.colors['text'], bg=self.colors['card'])
        mode_title.pack(anchor=tk.W, padx=15, pady=(10, 6))

        self.privacy_mode_var = tk.BooleanVar(value=self.privacy_mode)
        privacy_cb = tk.Checkbutton(
            mode_card_page,
            text="隐私模式（开启后不进入 Windows 通知中心，改为应用内提醒）",
            variable=self.privacy_mode_var,
            command=self._on_privacy_mode_changed,
            font=('Segoe UI', 9),
            fg='#D13438',
            bg=self.colors['card'],
            activebackground=self.colors['card']
        )
        privacy_cb.pack(anchor=tk.W, padx=15, pady=(2, 2))

        tk.Label(
            mode_card_page,
            text="系统通知模式：与系统深度整合，操作习惯一致；隐私模式：不进入通知中心，隐私更强。",
            font=('Segoe UI', 9),
            fg=self.colors['muted'],
            bg=self.colors['card']
        ).pack(anchor=tk.W, padx=15)

        tk.Label(
            mode_card_page,
            text="说明：系统通知历史可在 Windows 设置中清理（设置 > 系统 > 通知）。",
            font=('Segoe UI', 9),
            fg=self.colors['muted'],
            bg=self.colors['card']
        ).pack(anchor=tk.W, padx=15, pady=(2, 6))

        popup_row = tk.Frame(mode_card_page, bg=self.colors['card'])
        popup_row.pack(fill=tk.X, padx=15, pady=(2, 2))

        tk.Label(popup_row, text="弹窗停留时间（秒）:", font=('Segoe UI', 9), fg=self.colors['muted'], bg=self.colors['card']).pack(side=tk.LEFT)
        self.popup_duration_var = tk.StringVar(value=str(max(1, int(self.popup_duration_ms / 1000))))
        self.popup_duration_entry = tk.Entry(popup_row, textvariable=self.popup_duration_var, font=('Consolas', 9), width=8)
        self.popup_duration_entry.pack(side=tk.LEFT, padx=(8, 10))
        self.popup_duration_entry.bind('<FocusOut>', lambda e: self._save_privacy_config())

        self.popup_sound_var = tk.BooleanVar(value=self.popup_sound)
        popup_sound_cb = tk.Checkbutton(
            popup_row,
            text="弹窗声音提醒",
            variable=self.popup_sound_var,
            command=self._save_privacy_config,
            font=('Segoe UI', 9),
            bg=self.colors['card'],
            activebackground=self.colors['card']
        )
        popup_sound_cb.pack(side=tk.LEFT)

        storage_row = tk.Frame(mode_card_page, bg=self.colors['card'])
        storage_row.pack(fill=tk.X, padx=15, pady=(4, 2))

        tk.Label(storage_row, text="消息存储路径:", font=('Segoe UI', 9),
                 fg=self.colors['muted'], bg=self.colors['card']).pack(side=tk.LEFT)

        self.local_store_dir_var = tk.StringVar(value=self.local_store_dir)
        self.local_store_entry = tk.Entry(storage_row, textvariable=self.local_store_dir_var,
                                          font=('Consolas', 9))
        self.local_store_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6))
        self.local_store_entry.bind('<FocusOut>', lambda e: self._save_privacy_config())

        choose_store_btn = self._btn_subtle(storage_row, "选择", self._choose_local_store_dir)
        choose_store_btn.pack(side=tk.LEFT)

        action_row = tk.Frame(mode_card_page, bg=self.colors['card'])
        action_row.pack(fill=tk.X, padx=15, pady=(4, 0))

        check_size_btn = self._btn_subtle(action_row, "检测占用", self._check_local_store_size)
        check_size_btn.pack(side=tk.LEFT)

        clear_size_btn = self._btn_primary(action_row, "清理本地消息", self._clear_local_store, danger=True)
        clear_size_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.local_store_size_label = tk.Label(
            action_row,
            text="当前占用：未检测",
            font=('Segoe UI', 9),
            fg=self.colors['muted'],
            bg=self.colors['card']
        )
        self.local_store_size_label.pack(side=tk.LEFT, padx=(10, 0))

        self.content_security = tk.Frame(right_container, bg=self.colors['bg'])

        security_card = tk.Frame(self.content_security, bg=self.colors['card'],
                                 highlightbackground=self.colors['border'],
                                 highlightthickness=1)
        security_card.pack(fill=tk.X, pady=(0, 15), padx=20, ipady=10)

        security_header = tk.Label(security_card, text="🔐 安全配置（推荐）",
                                   font=('Segoe UI', 12, 'bold'),
                                   bg=self.colors['card'], fg=self.colors['text'])
        security_header.pack(anchor=tk.W, padx=15, pady=(10, 6))

        security_hint = tk.Label(
            security_card,
            text="提示：这里的修改会实时自动保存并立即生效，无需手动点击保存。",
            font=('Segoe UI', 9),
            fg=self.colors['muted'],
            bg=self.colors['card']
        )
        security_hint.pack(anchor=tk.W, padx=15, pady=(0, 8))

        token_frame = tk.Frame(security_card, bg=self.colors['card'])
        token_frame.pack(fill=tk.X, padx=15, pady=5)

        tk.Label(token_frame, text="安全Token:",
                 font=('Segoe UI', 10),
                 fg=self.colors['muted'], bg=self.colors['card']).pack(side=tk.LEFT)

        self.token_var = tk.StringVar(value=self.auth_token)
        self.token_entry = tk.Entry(token_frame, textvariable=self.token_var,
                                    font=('Consolas', 10), width=30, show='*')
        self.token_entry.pack(side=tk.LEFT, padx=(10, 8))
        self.token_entry.bind('<Control-c>', self._copy_token_event)
        self.token_entry.bind('<Control-C>', self._copy_token_event)
        self.token_entry.bind('<<Copy>>', self._copy_token_event)
        self.token_entry.bind('<FocusOut>', lambda e: self._autosave_token())
        self.token_var.trace_add('write', lambda *args: self._schedule_token_autosave())

        gen_token_btn = self._btn_subtle(token_frame, "生成", self._generate_token)
        gen_token_btn.pack(side=tk.LEFT)

        copy_token_btn = self._btn_subtle(token_frame, "复制", self._copy_token)
        copy_token_btn.pack(side=tk.LEFT, padx=(6, 0))

        crypto_frame = tk.Frame(security_card, bg=self.colors['card'])
        crypto_frame.pack(fill=tk.X, padx=15, pady=5)

        tk.Label(crypto_frame, text="加密密钥:",
                 font=('Segoe UI', 10),
                 fg=self.colors['muted'], bg=self.colors['card']).pack(side=tk.LEFT)

        self.crypto_var = tk.StringVar(value=self.crypto_key)
        self.crypto_entry = tk.Entry(crypto_frame, textvariable=self.crypto_var,
                                     font=('Consolas', 10), width=30, show='*')
        self.crypto_entry.pack(side=tk.LEFT, padx=(10, 8))
        self.crypto_entry.bind('<Control-c>', self._copy_crypto_event)
        self.crypto_entry.bind('<Control-C>', self._copy_crypto_event)
        self.crypto_entry.bind('<<Copy>>', self._copy_crypto_event)
        self.crypto_entry.bind('<FocusOut>', lambda e: self._autosave_crypto_key())
        self.crypto_var.trace_add('write', lambda *args: self._schedule_crypto_autosave())

        gen_crypto_btn = self._btn_subtle(crypto_frame, "生成", self._generate_crypto_key)
        gen_crypto_btn.pack(side=tk.LEFT)

        copy_crypto_btn = self._btn_subtle(crypto_frame, "复制", self._copy_crypto_key)
        copy_crypto_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.show_secret_var = tk.BooleanVar(value=False)
        show_secret_cb = tk.Checkbutton(security_card,
                                        text="显示 Token/密钥",
                                        variable=self.show_secret_var,
                                        command=self._toggle_secret_visibility,
                                        font=('Segoe UI', 9),
                                        bg=self.colors['card'],
                                        activebackground=self.colors['card'])
        show_secret_cb.pack(anchor=tk.W, padx=15, pady=(4, 8))

        author_wrap = tk.Frame(self.content_author, bg=self.colors['bg'])
        author_wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 15))

        self.author_canvas = tk.Canvas(author_wrap, bg=self.colors['bg'], highlightthickness=0)
        self.author_scroll = tk.Scrollbar(author_wrap, orient=tk.VERTICAL, command=self.author_canvas.yview)
        self.author_canvas.configure(yscrollcommand=self.author_scroll.set)

        self.author_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.author_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        author_card = tk.Frame(self.author_canvas, bg=self.colors['card'],
                               highlightbackground=self.colors['border'],
                               highlightthickness=1)
        self.author_canvas_window = self.author_canvas.create_window((0, 0), window=author_card, anchor='nw')

        author_card.bind('<Configure>', lambda e: self.author_canvas.configure(scrollregion=self.author_canvas.bbox('all')))
        self.author_canvas.bind('<Configure>', lambda e: self.author_canvas.itemconfigure(self.author_canvas_window, width=e.width))

        self._author_scroll_active = False
        self.author_canvas.bind('<Enter>', lambda e: self._set_author_scroll_active(True))
        self.author_canvas.bind('<Leave>', lambda e: self._set_author_scroll_active(False))
        author_card.bind('<Enter>', lambda e: self._set_author_scroll_active(True))
        author_card.bind('<Leave>', lambda e: self._set_author_scroll_active(False))

        tk.Label(author_card, text="👨‍💻 作者信息",
                 font=('Segoe UI', 13, 'bold'),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(12, 8))

        tk.Label(author_card, text="作者",
                 font=('Segoe UI', 10, 'bold'),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(2, 0))
        tk.Label(author_card, text="尼古拉.小侠碧青",
                 font=('Segoe UI', 10),
                 bg=self.colors['card'], fg=self.colors['muted']).pack(anchor=tk.W, padx=16, pady=(2, 8))

        tk.Label(author_card, text="联系方式",
                 font=('Segoe UI', 10, 'bold'),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(2, 0))

        contact_text = (
            "微信：xiabiqing1\n"
            "QQ：2632493933\n"
            "Github：https://github.com/xiabiqing"
        )
        tk.Label(author_card, text=contact_text,
                 justify=tk.LEFT,
                 font=('Segoe UI', 10),
                 bg=self.colors['card'], fg=self.colors['muted']).pack(anchor=tk.W, padx=16, pady=(3, 10))

        tk.Label(author_card, text="开发初衷",
                 font=('Segoe UI', 10, 'bold'),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(2, 0))

        origin_text = (
            "日常使用中，除了微信、QQ，很多手机应用（例如 Boss、闲鱼、验证码等）的消息在电脑端没有对应提醒。\n"
            "为了减少频繁拿起手机查看消息的麻烦，我结合 AI 开发了 NotifySync，\n"
            "让手机通知可以同步到电脑端，提升日常沟通和处理效率。\n\n"
            "本项目已开源，欢迎使用。\n"
            "如果你在使用中遇到问题，或有优化建议，欢迎在 Github 提 issue；\n"
            "也欢迎基于本项目进行二次开发，共同完善。"
        )
        tk.Label(author_card, text=origin_text,
                 justify=tk.LEFT,
                 wraplength=760,
                 font=('Segoe UI', 10),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(3, 10))

        tk.Label(author_card, text="────────────────────────────────────────",
                 font=('Segoe UI', 9),
                 bg=self.colors['card'], fg=self.colors['border']).pack(anchor=tk.W, padx=16, pady=(2, 6))

        tk.Label(author_card, text="项目安全性说明",
                 font=('Segoe UI', 10, 'bold', 'underline'),
                 bg=self.colors['card'], fg=self.colors['text']).pack(anchor=tk.W, padx=16, pady=(0, 2))

        security_text = (
            "1) 默认局域网使用：建议仅在同一内网环境下使用，减少公网暴露风险。\n"
            "2) 双重校验：支持 Authorization Bearer 与 X-NotifySync-Token 校验机制。\n"
            "3) 可选内容加密：支持加密密钥，开启后可避免明文传输。\n"
            "4) 隐私模式：可关闭系统通知中心，改为应用内提醒，降低系统侧通知残留。\n"
            "5) 本地可控：支持自定义消息存储路径、占用检测与一键清理。\n"
            "6) 安全建议：请定期更换 Token/密钥，不要将服务端口暴露到公网。"
        )
        tk.Label(author_card, text=security_text,
                 justify=tk.LEFT,
                 wraplength=760,
                 font=('Segoe UI', 9),
                 bg=self.colors['card'], fg=self.colors['muted']).pack(anchor=tk.W, padx=16, pady=(2, 12))

        # 日志区域
        self.log_card = tk.Frame(self.content_logs, bg=self.colors['card'],
                                 highlightbackground=self.colors['border'],
                                 highlightthickness=1)
        self.log_card.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

        log_header_row = tk.Frame(self.log_card, bg=self.colors['card'])
        log_header_row.pack(fill=tk.X, padx=15, pady=(10, 5))

        log_header = tk.Label(log_header_row, text="📋 运行日志",
                              font=('Segoe UI', 13, 'bold'),
                              bg=self.colors['card'], fg=self.colors['text'])
        log_header.pack(side=tk.LEFT)

        self.log_visible_var = tk.BooleanVar(value=True)
        self.toggle_log_btn = self._btn_subtle(log_header_row, "收起日志", self._toggle_log_panel)
        self.toggle_log_btn.pack(side=tk.RIGHT)

        clear_log_btn = self._btn_subtle(log_header_row, "一键清空日志", self._clear_logs)
        clear_log_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self.log_content_frame = tk.Frame(self.log_card, bg=self.colors['card'])
        self.log_content_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 10))

        # 日志文本框（显式滚动条，支持横向滚动）
        log_text_wrap = tk.Frame(self.log_content_frame, bg=self.colors['card'])
        log_text_wrap.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_text_wrap,
            wrap=tk.NONE,
            font=('Consolas', 10),
            bg='#111111',
            fg='#F3F2F1',
            insertbackground='white',
            relief=tk.FLAT,
            padx=10,
            pady=10,
            height=16
        )
        self.log_text.grid(row=0, column=0, sticky='nsew')

        self.log_v_scroll = tk.Scrollbar(log_text_wrap, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_v_scroll.grid(row=0, column=1, sticky='ns')

        self.log_h_scroll = tk.Scrollbar(log_text_wrap, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_h_scroll.grid(row=1, column=0, sticky='ew')

        self.log_text.configure(yscrollcommand=self.log_v_scroll.set, xscrollcommand=self.log_h_scroll.set)

        log_text_wrap.grid_rowconfigure(0, weight=1)
        log_text_wrap.grid_columnconfigure(0, weight=1)

        # 鼠标滚轮支持
        self.log_text.bind('<MouseWheel>', lambda e: self.log_text.yview_scroll(int(-e.delta / 120), 'units'))
        self.log_text.bind('<Shift-MouseWheel>', lambda e: self.log_text.xview_scroll(int(-e.delta / 120), 'units'))

        # 底部按钮
        bottom_frame = ttk.Frame(self.content_basic_inner)
        bottom_frame.pack(fill=tk.X, pady=(4, 0))

        about_btn = self._btn_subtle(bottom_frame, "关于", self._show_about)
        about_btn.config(padx=16, pady=5)
        about_btn.pack(side=tk.LEFT)

        dev_label = tk.Label(bottom_frame,
                             text="软件开发者：尼古拉.小侠碧青",
                             font=('Microsoft YaHei', 9),
                             fg=self.colors['muted'])
        dev_label.pack(side=tk.LEFT, padx=(12, 0))

        exit_btn = self._btn_primary(bottom_frame, "退出程序", self._exit_app, danger=True)
        exit_btn.config(padx=16, pady=5)
        exit_btn.pack(side=tk.RIGHT)

        # 默认显示基础设置页
        self._show_left_page('basic')

    def _show_left_page(self, page: str):
        self.content_basic.pack_forget()
        self.content_logs.pack_forget()
        self.content_mode.pack_forget()
        self.content_security.pack_forget()
        self.content_author.pack_forget()

        self.nav_btn_basic.config(bg='#FAFAFA', fg=self.colors['text'], font=('Segoe UI', 10))
        self.nav_btn_logs.config(bg='#FAFAFA', fg=self.colors['text'], font=('Segoe UI', 10))
        self.nav_btn_mode.config(bg='#FAFAFA', fg=self.colors['text'], font=('Segoe UI', 10))
        self.nav_btn_security.config(bg='#FAFAFA', fg=self.colors['text'], font=('Segoe UI', 10))
        self.nav_btn_author.config(bg='#FAFAFA', fg=self.colors['text'], font=('Segoe UI', 10))

        if page == 'logs':
            self.content_logs.pack(fill=tk.BOTH, expand=True, padx=20, pady=(6, 16))
            self.nav_btn_logs.config(bg='#E8F3FC', fg=self.colors['primary'], font=('Segoe UI', 10, 'bold'))
        elif page == 'mode':
            self.content_mode.pack(fill=tk.BOTH, expand=True, padx=0, pady=(6, 16))
            self.nav_btn_mode.config(bg='#E8F3FC', fg=self.colors['primary'], font=('Segoe UI', 10, 'bold'))
        elif page == 'security':
            self.content_security.pack(fill=tk.BOTH, expand=True, padx=0, pady=(6, 16))
            self.nav_btn_security.config(bg='#E8F3FC', fg=self.colors['primary'], font=('Segoe UI', 10, 'bold'))
        elif page == 'author':
            self.content_author.pack(fill=tk.BOTH, expand=True, padx=0, pady=(6, 16))
            self.nav_btn_author.config(bg='#E8F3FC', fg=self.colors['primary'], font=('Segoe UI', 10, 'bold'))
        else:
            self.content_basic.pack(fill=tk.BOTH, expand=True, padx=20, pady=(6, 16))
            self.nav_btn_basic.config(bg='#E8F3FC', fg=self.colors['primary'], font=('Segoe UI', 10, 'bold'))

    def _apply_button_style(self, btn, style='secondary'):
        """统一按钮视觉风格（微软风格）"""
        palette = {
            'primary': ('#0F6CBD', 'white', '#115EA3'),
            'secondary': ('#605E5C', 'white', '#4A4846'),
            'danger': ('#D13438', 'white', '#A4262C'),
            'success': ('#107C10', 'white', '#0B6A0B')
        }
        bg, fg, active = palette.get(style, palette['secondary'])
        btn.config(
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            cursor='hand2'
        )

    def _set_basic_scroll_active(self, active: bool):
        self._basic_scroll_active = bool(active)

    def _on_basic_mousewheel(self, event):
        try:
            delta_units = int(-event.delta / 120) if event.delta else 0
            if delta_units == 0:
                return None

            # 基础设置页：鼠标在应用任意位置都可滚动
            if self.content_basic.winfo_ismapped():
                self.basic_canvas.yview_scroll(delta_units, 'units')
                return 'break'

            # 作者信息页：鼠标在应用任意位置都可滚动
            if hasattr(self, 'content_author') and self.content_author.winfo_ismapped():
                self.author_canvas.yview_scroll(delta_units, 'units')
                return 'break'

            # 日志页：统一滚动日志面板
            if self.content_logs.winfo_ismapped():
                self.log_text.yview_scroll(delta_units, 'units')
                return 'break'
        except Exception:
            return None
        return None

    def _apply_window_icon(self):
        """设置窗口图标（优先最新 ico，支持 icon 子目录）"""
        ico_candidates = get_icon_candidates('ico')
        png_candidates = get_icon_candidates('png')

        # 窗口/任务栏优先使用ICO，PNG作为回退
        icon_path = None
        icon_type = None

        for path in ico_candidates:
            if os.path.exists(path):
                icon_path = path
                icon_type = 'ico'
                break

        if not icon_path:
            for path in png_candidates:
                if os.path.exists(path):
                    icon_path = path
                    icon_type = 'png'
                    break

        if not icon_path:
            logger.warning('未找到可用的窗口图标文件')
            return

        try:
            # 使用Windows API设置任务栏图标
            if sys.platform == 'win32' and user32:
                self._set_windows_taskbar_icon(icon_path)
                logger.info(f"Windows任务栏图标已设置: {icon_path}")
            else:
                # 非Windows系统使用tkinter方法
                if icon_type == 'ico':
                    self.root.iconbitmap(icon_path)
                else:
                    icon_img = tk.PhotoImage(file=icon_path)
                    self.root.iconphoto(True, icon_img)
                    self.root._ns_icon_ref = icon_img
                logger.info(f"窗口图标已加载: {icon_path}")
        except Exception as e:
            logger.warning(f"设置窗口图标失败: {e}")

    def _set_windows_taskbar_icon(self, icon_path):
        """使用Windows API设置任务栏图标"""
        # 获取窗口句柄
        hwnd = self.root.winfo_id()

        # 加载图标
        if icon_path.endswith('.ico'):
            # 使用ICO文件
            hicon = user32.LoadImageW(
                None, icon_path, 1,  # 1 = IMAGE_ICON
                0, 0,  # 使用实际尺寸
                0x00000010  # LR_LOADFROMFILE
            )
        else:
            # 对于PNG，我们需要先转换为ICON
            # 这里使用PIL加载然后创建临时ICO
            from PIL import Image
            img = Image.open(icon_path).convert('RGBA')
            # 调整大小为任务栏图标常用尺寸
            img = img.resize((32, 32), Image.LANCZOS)

            # 保存为临时ICO
            import tempfile
            temp_ico = os.path.join(tempfile.gettempdir(), 'notifysync_temp.ico')

            # 创建简单的ICO文件
            img.save(temp_ico, format='ICO', sizes=[(32, 32)])

            hicon = user32.LoadImageW(
                None, temp_ico, 1,
                0, 0,
                0x00000010
            )

        if hicon:
            # 设置大图标和小图标
            # GCLP_HICON = -14, GCLP_HICONSM = -34
            user32.SetClassLongPtrW(hwnd, -14, hicon)  # 大图标
            user32.SetClassLongPtrW(hwnd, -34, hicon)  # 小图标

            # 同时设置tkinter图标
            if icon_path.endswith('.ico'):
                self.root.iconbitmap(icon_path)
            else:
                icon_img = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, icon_img)
                self.root._ns_icon_ref = icon_img
        else:
            # 如果API调用失败，回退到tkinter方法
            if icon_path.endswith('.ico'):
                self.root.iconbitmap(icon_path)
            else:
                icon_img = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, icon_img)
                self.root._ns_icon_ref = icon_img

    def _show_network_info(self):
        """显示网络信息"""
        ips = get_ip_addresses()
        recommended = choose_recommended_ip(ips)

        if recommended:
            self.current_ip = f"{recommended}:{PORT}"
            self.ip_label.config(text=self.current_ip)
            self.add_log(f"推荐使用的地址: {self.current_ip}")
            self.add_log(f"所有可用IP: {', '.join(ips)}")
        else:
            self.ip_label.config(text="未找到IP地址")
            self.add_log("警告: 未找到可用的IPv4地址")

    def _start_server(self):
        """启动服务"""
        try:
            self.server.start()
            self.add_log("服务端已启动，等待手机连接...")
        except Exception as e:
            self.add_log(f"启动失败: {e}")
            messagebox.showerror("错误", f"无法启动服务: {e}")

    def _toggle_server(self):
        """切换服务状态"""
        if self.server.running:
            self.server.stop()
            self.status_dot.config(fg='#FF5252')
            self.status_text.config(text="服务已停止", fg='#FF5252')
            self.toggle_btn.config(text="启动服务", bg=self.colors['success'])
            self.add_log("服务已停止")
        else:
            self._start_server()
            self.status_dot.config(fg=self.colors['success'])
            self.status_text.config(text="服务运行中", fg=self.colors['success'])
            self.toggle_btn.config(text="停止服务", bg='#FF5252')

    def _copy_ip(self):
        """复制IP到剪贴板"""
        if hasattr(self, 'current_ip'):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.current_ip)
            self.add_log(f"已复制到剪贴板: {self.current_ip}")
            messagebox.showinfo("提示", "地址已复制到剪贴板，请在手机端粘贴")

    def _send_test(self):
        """发送测试通知"""
        show_notification("NotifySync", "测试通知", "电脑端通知功能正常工作！")
        self.add_log("已发送测试通知到Windows通知栏")

    def _on_app_seen(self, app_name, package_name):
        pkg = _normalize_pkg_name(package_name)
        if not pkg:
            return
        name = (app_name or '').strip() or pkg
        if self.known_apps.get(pkg) != name:
            self.known_apps[pkg] = name
            self.config['known_apps'] = dict(self.known_apps)
            save_app_config(self.config)
            self.root.after(0, self._refresh_app_policy_rows)

    def _build_known_app_order(self):
        default_order = [
            ('com.tencent.mm', '微信'),
            ('com.tencent.mobileqq', 'QQ'),
            ('com.tencent.wework', '企业微信'),
            ('com.tencent.tim', 'TIM'),
            ('com.alibaba.android.rimet', '钉钉'),
        ]
        for pkg, name in default_order:
            if pkg not in self.known_apps:
                self.known_apps[pkg] = name

        items = list(self.known_apps.items())
        items.sort(key=lambda x: x[1])
        return items

    def _on_toggle_app_policy(self, pkg, var):
        self.app_mute_overrides[pkg] = bool(var.get())
        self._save_smart_mute_config()
        self.add_log(f"应用策略已更新: {self.known_apps.get(pkg, pkg)} -> {'静默' if var.get() else '提醒'}")

    def _set_all_app_policies_on(self):
        for pkg, _ in self._build_known_app_order():
            self.app_mute_overrides[pkg] = True
        self._save_smart_mute_config()
        self._refresh_app_policy_rows()
        self.add_log('已将全部应用策略设置为静默')

    def _set_all_app_policies_off(self):
        for pkg, _ in self._build_known_app_order():
            self.app_mute_overrides[pkg] = False
        self._save_smart_mute_config()
        self._refresh_app_policy_rows()
        self.add_log('已将全部应用策略设置为提醒')

    def _refresh_app_policy_rows(self):
        if not hasattr(self, 'app_policy_inner'):
            return

        for w in self.app_policy_inner.winfo_children():
            w.destroy()

        items = self._build_known_app_order()
        for pkg, name in items:
            row = tk.Frame(self.app_policy_inner, bg=self.colors['card'])
            row.pack(fill=tk.X, pady=3)

            title = tk.Label(row, text=name, font=('Segoe UI', 10, 'bold'), bg=self.colors['card'], fg=self.colors['text'])
            title.pack(side=tk.LEFT)

            subtitle = tk.Label(row, text=f"  {pkg}", font=('Consolas', 9), bg=self.colors['card'], fg=self.colors['muted'])
            subtitle.pack(side=tk.LEFT)

            is_mute = bool(self.app_mute_overrides.get(pkg, False))
            var = tk.BooleanVar(value=is_mute)
            cb = tk.Checkbutton(
                row,
                text='静默',
                variable=var,
                command=lambda p=pkg, v=var: self._on_toggle_app_policy(p, v),
                font=('Segoe UI', 9),
                bg=self.colors['card'],
                activebackground=self.colors['card']
            )
            cb.pack(side=tk.RIGHT)

    def _generate_token(self):
        """生成随机安全Token"""
        token = secrets.token_urlsafe(24)
        self.token_var.set(token)
        self.add_log("已生成随机Token")
        self._autosave_token(show_message=False)

    def _toggle_secret_visibility(self):
        show = '' if self.show_secret_var.get() else '*'
        self.token_entry.config(show=show)
        self.crypto_entry.config(show=show)

    def _schedule_token_autosave(self):
        if hasattr(self, '_token_autosave_job') and self._token_autosave_job:
            try:
                self.root.after_cancel(self._token_autosave_job)
            except Exception:
                pass
        self._token_autosave_job = self.root.after(500, lambda: self._autosave_token(show_message=False))

    def _autosave_token(self, show_message: bool = False):
        self._token_autosave_job = None
        token = self.token_var.get().strip()
        if token == self.auth_token:
            return True

        self.auth_token = token
        self.server.auth_token = token
        self.config['auth_token'] = token
        if save_app_config(self.config):
            if token:
                self.add_log("安全Token已自动保存并生效")
                if show_message:
                    messagebox.showinfo("提示", "Token 已自动保存")
            else:
                self.add_log("安全Token已清空（不校验）")
                if show_message:
                    messagebox.showinfo("提示", "Token 已清空")
            return True

        messagebox.showerror("错误", "自动保存 Token 失败")
        return False

    def _generate_crypto_key(self):
        """生成随机加密密钥"""
        key = secrets.token_urlsafe(32)
        self.crypto_var.set(key)
        self.add_log("已生成随机加密密钥")
        self._autosave_crypto_key(show_message=False)

    def _schedule_crypto_autosave(self):
        if hasattr(self, '_crypto_autosave_job') and self._crypto_autosave_job:
            try:
                self.root.after_cancel(self._crypto_autosave_job)
            except Exception:
                pass
        self._crypto_autosave_job = self.root.after(500, lambda: self._autosave_crypto_key(show_message=False))

    def _autosave_crypto_key(self, show_message: bool = False):
        self._crypto_autosave_job = None
        key = self.crypto_var.get().strip()
        if key == self.crypto_key:
            return True

        self.crypto_key = key
        self.server.crypto_key = key
        self.config['crypto_key'] = key
        if save_app_config(self.config):
            if key:
                self.add_log("加密密钥已自动保存并生效")
                if show_message:
                    messagebox.showinfo("提示", "加密密钥已自动保存")
            else:
                self.add_log("加密密钥已清空（明文传输）")
                if show_message:
                    messagebox.showinfo("提示", "加密密钥已清空")
            return True

        messagebox.showerror("错误", "自动保存加密密钥失败")
        return False

    def _copy_token(self):
        """一键复制安全Token"""
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("提示", "Token 为空，无法复制")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(token)
        self.add_log("已复制安全Token到剪贴板")

    def _copy_crypto_key(self):
        """一键复制加密密钥"""
        key = self.crypto_var.get().strip()
        if not key:
            messagebox.showwarning("提示", "加密密钥为空，无法复制")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(key)
        self.add_log("已复制加密密钥到剪贴板")

    def _copy_token_event(self, event):
        """拦截 Token 输入框复制，始终复制真实值"""
        token = self.token_var.get().strip()
        if token:
            self.root.clipboard_clear()
            self.root.clipboard_append(token)
            self.add_log("已复制安全Token到剪贴板")
        return 'break'

    def _copy_crypto_event(self, event):
        """拦截密钥输入框复制，始终复制真实值"""
        key = self.crypto_var.get().strip()
        if key:
            self.root.clipboard_clear()
            self.root.clipboard_append(key)
            self.add_log("已复制加密密钥到剪贴板")
        return 'break'

    def _save_privacy_config(self):
        self.privacy_mode = bool(self.privacy_mode_var.get()) if hasattr(self, 'privacy_mode_var') else self.privacy_mode
        self.popup_sound = bool(self.popup_sound_var.get()) if hasattr(self, 'popup_sound_var') else self.popup_sound

        if hasattr(self, 'popup_duration_var'):
            raw = self.popup_duration_var.get().strip()
            try:
                sec = int(raw)
            except Exception:
                sec = max(1, int(self.popup_duration_ms / 1000))
            sec = max(1, min(60, sec))
            self.popup_duration_ms = sec * 1000
            self.popup_duration_var.set(str(sec))

        path_text = self.local_store_dir_var.get().strip() if hasattr(self, 'local_store_dir_var') else self.local_store_dir
        if path_text:
            self.local_store_dir = path_text

        self.config['privacy_mode'] = self.privacy_mode
        self.config['popup_duration_ms'] = self.popup_duration_ms
        self.config['popup_sound'] = self.popup_sound
        self.config['local_store_dir'] = self.local_store_dir
        save_app_config(self.config)

    def _on_privacy_mode_changed(self):
        self._save_privacy_config()
        mode = '隐私模式（应用内提醒）' if self.privacy_mode else '系统通知模式（Windows通知中心）'
        self.add_log(f"通知模式已切换：{mode}")

    def _format_size(self, size_bytes: int):
        size = float(size_bytes)
        units = ['B', 'KB', 'MB', 'GB']
        for u in units:
            if size < 1024 or u == units[-1]:
                return f"{size:.1f}{u}" if u != 'B' else f"{int(size)}B"
            size /= 1024.0

    def _calc_dir_size(self, path: str):
        total = 0
        files = 0
        if not os.path.isdir(path):
            return 0, 0
        for root, _, names in os.walk(path):
            for n in names:
                fp = os.path.join(root, n)
                try:
                    total += os.path.getsize(fp)
                    files += 1
                except Exception:
                    pass
        return total, files

    def _check_local_store_size(self):
        self._save_privacy_config()
        size, files = self._calc_dir_size(self.local_store_dir)
        text = f"当前占用：{self._format_size(size)}（{files}个文件）"
        self.local_store_size_label.config(text=text)
        self.add_log(f"本地消息占用检测：{text}")

    def _clear_local_store(self):
        self._save_privacy_config()
        if not os.path.isdir(self.local_store_dir):
            messagebox.showinfo('提示', '目录不存在，无需清理')
            return
        if not messagebox.askyesno('确认', '确定清理本地消息存储目录吗？'):
            return
        try:
            for name in os.listdir(self.local_store_dir):
                p = os.path.join(self.local_store_dir, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            self._check_local_store_size()
            self.add_log('已清理本地消息存储目录')
            messagebox.showinfo('提示', '本地消息已清理')
        except Exception as e:
            messagebox.showerror('错误', f'清理失败: {e}')

    def _choose_local_store_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.local_store_dir or os.path.expanduser('~'))
        if not chosen:
            return
        self.local_store_dir_var.set(chosen)
        self._save_privacy_config()
        self.add_log(f"本地消息存储路径已更新: {chosen}")

    def _show_in_app_toast(self, app_name: str, title: str, text: str, sub_text: str):
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        try:
            top.attributes('-alpha', 0.98)
        except Exception:
            pass
        top.configure(bg='#0F0F10')

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w, h = 420, 128

        work = get_windows_work_area()
        if work:
            left, top_y, right, bottom = work
            x = max(left + 8, right - w - 16)
            y = max(top_y + 8, bottom - h - 16)
        else:
            x = max(20, screen_w - w - 24)
            y = max(20, screen_h - h - 72)

        top.geometry(f"{w}x{h}+{x}+{y}")

        card = tk.Frame(top, bg='#171717', highlightthickness=1, highlightbackground='#2A2A2A')
        card.pack(fill=tk.BOTH, expand=True)

        title_text = f"{app_name}" + (f" · {title}" if title else '')
        body_text = (text or '').strip() or '(无内容)'
        if sub_text and sub_text != text:
            body_text = f"{body_text}\n{sub_text}"

        header = tk.Frame(card, bg='#171717')
        header.pack(fill=tk.X, padx=12, pady=(10, 4))

        tk.Label(
            header,
            text=title_text,
            font=('Segoe UI', 10, 'bold'),
            fg='#F3F2F1',
            bg='#171717',
            anchor='w'
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        close_btn = tk.Button(
            header,
            text='×',
            command=top.destroy,
            font=('Segoe UI', 11, 'bold'),
            bg='#171717',
            fg='#B3B0AD',
            relief=tk.FLAT,
            bd=0,
            padx=6,
            pady=0,
            activebackground='#2A2A2A',
            activeforeground='#FFFFFF',
            cursor='hand2'
        )
        close_btn.pack(side=tk.RIGHT)

        tk.Label(
            card,
            text=body_text,
            font=('Segoe UI', 9),
            fg='#E1DFDD',
            bg='#171717',
            anchor='w',
            justify=tk.LEFT,
            wraplength=392
        ).pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        if self.popup_sound:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

        top.after(max(1000, int(self.popup_duration_ms)), top.destroy)

    def _persist_local_message(self, app_name: str, title: str, text: str, sub_text: str, package_name: str):
        self._save_privacy_config()
        os.makedirs(self.local_store_dir, exist_ok=True)
        filename = datetime.now().strftime('%Y%m%d') + '.log'
        path = os.path.join(self.local_store_dir, filename)
        payload = {
            'ts': datetime.now().isoformat(),
            'app': app_name,
            'title': title,
            'text': text,
            'sub_text': sub_text,
            'package': package_name,
        }
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def _handle_notification_delivery(self, app_name: str, title: str, text: str, sub_text: str, package_name: str):
        if self.privacy_mode:
            self.root.after(0, lambda: self._show_in_app_toast(app_name, title, text, sub_text))
            try:
                self._persist_local_message(app_name, title, text, sub_text, package_name)
            except Exception as e:
                self.add_log(f"本地消息记录失败: {e}")
            return True
        return False

    def _on_autostart_changed(self):
        """自启动选项变更"""
        enabled = self.autostart_var.get()
        ok = set_autostart(enabled)
        really_enabled = is_autostart_enabled()
        self.autostart_var.set(really_enabled)

        if ok:
            status = "已启用" if really_enabled else "已禁用"
            self.add_log(f"开机自启动: {status}")
            if enabled and not really_enabled:
                messagebox.showwarning("提示", "系统未保留自启动项，请检查安全策略或权限设置")
        else:
            self.add_log("开机自启动设置失败")
            messagebox.showerror("错误", "设置开机自启动失败")

    def _toggle_log_panel(self):
        """展开/收起日志面板"""
        visible = self.log_visible_var.get()
        if visible:
            self.log_content_frame.pack_forget()
            self.toggle_log_btn.config(text="展开日志")
            self.log_visible_var.set(False)
        else:
            self.log_content_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 10))
            self.toggle_log_btn.config(text="收起日志")
            self.log_visible_var.set(True)

    def _clear_logs(self):
        """一键清空日志"""
        self.log_text.delete('1.0', tk.END)
        self.add_log("日志已清空")

    def add_log(self, message: str):
        """添加日志（线程安全）"""
        def _append():
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
            self.log_text.see(tk.END)

        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.root.after(0, _append)

    def _show_about(self):
        """显示关于对话框"""
        messagebox.showinfo("关于 NotifySync",
            "NotifySync v1.1\n\n"
            "将安卓手机通知实时同步到 Windows 通知栏\n\n"
            "软件开发者：尼古拉.小侠碧青\n\n"
            "使用方法:\n"
            "1. 确保手机和电脑在同一网络\n"
            "2. 在安卓端输入电脑显示的IP地址\n"
            "3. 授权安卓端的通知访问权限\n"
            "4. 手机收到通知时，电脑会自动显示")

    def _exit_app(self):
        """完全退出程序"""
        if messagebox.askyesno("确认", "确定要退出 NotifySync 吗？"):
            self._cleanup()
            self.root.destroy()
            sys.exit(0)

    def _on_close(self):
        """关闭窗口时处理"""
        if self.tray_var.get() and PYSTRAY_AVAILABLE:
            self._minimize_to_tray()
        else:
            self._exit_app()

    def _restore_window(self):
        """从托盘恢复窗口（确保前台显示、可拖动可缩放）"""
        self.root.deiconify()
        self.root.state('normal')
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after(200, lambda: self.root.attributes('-topmost', False))
        self.root.focus_force()
        self.add_log("已从系统托盘恢复窗口")

    def _minimize_to_tray(self):
        """最小化到系统托盘"""
        self.root.withdraw()  # 隐藏窗口

        if not self.tray_icon:
            def on_show(icon, item):
                self.root.after(0, self._restore_window)

            def on_exit(icon, item):
                icon.stop()
                self.root.after(0, self._exit_app)

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", on_show, default=True),
                pystray.MenuItem("退出", on_exit)
            )

            self.tray_icon = pystray.Icon(
                "NotifySync",
                create_tray_icon(),
                "NotifySync - 手机通知同步（双击托盘图标可恢复）",
                menu
            )

            # 托盘图标后台运行，默认菜单项支持双击恢复窗口
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()

        self.add_log("程序已最小化到系统托盘（双击托盘图标可直接显示窗口）")

    def _cleanup(self):
        """清理资源"""
        if self.server.running:
            self.server.stop()
        if self.tray_icon:
            self.tray_icon.stop()

    def run(self):
        """运行应用"""
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=PORT, help='服务器端口')
    args = parser.parse_args()

    if not PYSTRAY_AVAILABLE:
        print("提示: 安装 pystray 和 pillow 可启用系统托盘功能")
        print("pip install pystray pillow")

    app = NotifySyncGUI()
    app.run()


if __name__ == '__main__':
    main()
