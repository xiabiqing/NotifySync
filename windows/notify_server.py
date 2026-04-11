#!/usr/bin/env python3
"""
NotifySync Windows Server
接收安卓手机通知并显示在 Windows 通知栏
支持命令行模式与图形界面模式
"""

import argparse
import json
import logging
import socket
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('NotifySync')

HOST = "0.0.0.0"
PORT = 8787


class NotifySyncServer:
    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self.httpd = None
        self.thread = None
        self.running = False
        self.log_callback = None

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

    def _build_handler(self):
        outer = self

        class NotificationHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                if self.path != '/notify':
                    self.send_error(404)
                    return

                try:
                    content_length = int(self.headers.get('Content-Length', 0))
                    post_data = self.rfile.read(content_length)
                    data = json.loads(post_data.decode('utf-8'))

                    app_name = data.get('appName', '未知应用')
                    title = data.get('title', '')
                    text = data.get('text', '')
                    sub_text = data.get('subText', '')
                    package_name = data.get('packageName', '')

                    show_notification(app_name, title, text, sub_text)

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'ok'}).encode())

                    outer.log(f"收到通知: {app_name} | {title or '(无标题)'} | {package_name}")

                except json.JSONDecodeError as e:
                    outer.log(f"JSON 解析错误: {e}")
                    self.send_error(400, "Invalid JSON")
                except Exception as e:
                    outer.log(f"处理请求错误: {e}")
                    self.send_error(500, str(e))

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
            notify(
                title=final_title,
                body=body,
                app_id="NotifySync",
                duration="short"
            )
        elif TOAST_BACKEND == "win10toast":
            _toaster.show_toast(title=final_title, msg=body, duration=5, threaded=True)
        elif TOAST_BACKEND == "plyer":
            notification.notify(title=final_title, message=body, timeout=5)
        else:
            logger.info(f"[{app_name}] {title}: {body}")
    except Exception as e:
        logger.error(f"显示通知失败: {e}")


def get_ip_addresses():
    """使用 ipconfig 命令获取真实的 IPv4 地址，与系统显示一致"""
    ips = []
    try:
        import subprocess
        import re

        # 执行 ipconfig 命令
        result = subprocess.run(['ipconfig'], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        output = result.stdout

        # 提取 IPv4 地址（匹配 ipconfig 输出格式）
        # 匹配 "IPv4 地址" 或 "IPv4 Address" 后面的 IP
        ipv4_pattern = r'IPv4[^\d]*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        matches = re.findall(ipv4_pattern, output, re.IGNORECASE)

        for ip in matches:
            # 排除回环地址
            if not ip.startswith('127.'):
                ips.append(ip)

    except Exception as e:
        logger.warning(f"ipconfig 获取 IP 失败: {e}")
        # 降级方案：使用 socket 方式
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
    """推荐优先级：192.168.x.x > 10.x.x.x > 172.16-31.x.x > 其他"""
    if not ips:
        return None

    # 按优先级排序
    preferred_prefixes = [
        '192.168.',  # 最常用，家庭/热点网络
        '10.',
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


def run_cli(port: int):
    print("=" * 50)
    print("  NotifySync Windows Server")
    print("=" * 50)
    print()
    print(f"通知后端: {TOAST_BACKEND or 'console'}")

    ips = get_ip_addresses()
    recommended_ip = choose_recommended_ip(ips)
    print("本机 IPv4 地址:")
    for ip in ips:
        marker = "  <-- 推荐填写" if ip == recommended_ip else ""
        print(f"  - {ip}{marker}")

    if recommended_ip:
        print(f"\n安卓端建议填写: {recommended_ip}:{port}")
        print(f"测试地址: http://{recommended_ip}:{port}/test")

    server = NotifySyncServer(port=port)
    server.start()

    print("\n按 Ctrl+C 停止服务器")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\n服务器已停止")
        server.stop()


def run_gui(port: int):
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App:
        def __init__(self, root):
            self.root = root
            self.root.title("NotifySync 小白版")
            self.root.geometry("760x520")
            self.server = NotifySyncServer(port=port)
            self.server.log_callback = self.add_log

            self._build_ui()
            self._show_network_info()

        def _build_ui(self):
            top = ttk.Frame(self.root, padding=12)
            top.pack(fill=tk.X)

            ttk.Label(top, text="状态:").grid(row=0, column=0, sticky=tk.W)
            self.status_var = tk.StringVar(value="未启动")
            ttk.Label(top, textvariable=self.status_var, foreground="#b00020").grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

            self.addr_var = tk.StringVar(value="-")
            ttk.Label(top, text="手机填写地址:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
            ttk.Entry(top, textvariable=self.addr_var, state="readonly", width=46).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))

            btns = ttk.Frame(self.root, padding=(12, 0, 12, 0))
            btns.pack(fill=tk.X)
            ttk.Button(btns, text="启动服务", command=self.start_server).pack(side=tk.LEFT)
            ttk.Button(btns, text="停止服务", command=self.stop_server).pack(side=tk.LEFT, padx=8)
            ttk.Button(btns, text="发送测试通知", command=self.test_notification).pack(side=tk.LEFT)
            ttk.Button(btns, text="复制手机地址", command=self.copy_address).pack(side=tk.LEFT, padx=8)

            tip = (
                "使用步骤：\n"
                "1) 点击“启动服务”\n"
                "2) 把“手机填写地址”填到手机 App\n"
                "3) 手机端保存并测试\n"
                "4) 在下方日志查看是否收到通知"
            )
            ttk.Label(self.root, text=tip, padding=12, foreground="#333333").pack(anchor=tk.W)

            log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
            log_frame.pack(fill=tk.BOTH, expand=True)
            ttk.Label(log_frame, text="实时日志:").pack(anchor=tk.W)

            self.log_text = tk.Text(log_frame, height=18, wrap=tk.WORD)
            self.log_text.pack(fill=tk.BOTH, expand=True)
            self.log_text.configure(state=tk.DISABLED)

            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        def _show_network_info(self):
            ips = get_ip_addresses()
            rec = choose_recommended_ip(ips)
            if rec:
                self.addr_var.set(f"{rec}:{port}")
                self.add_log(f"推荐地址: {rec}:{port}")
            else:
                self.addr_var.set("未获取到IP")
                self.add_log("未获取到本机 IP，请检查网络")

        def add_log(self, msg: str):
            now = datetime.now().strftime("%H:%M:%S")
            line = f"[{now}] {msg}\n"
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        def start_server(self):
            try:
                self.server.start()
                self.status_var.set("运行中")
            except Exception as e:
                messagebox.showerror("启动失败", str(e))
                self.add_log(f"启动失败: {e}")

        def stop_server(self):
            self.server.stop()
            self.status_var.set("未启动")

        def test_notification(self):
            show_notification("NotifySync", "测试通知", "如果你看到了这条通知，说明电脑端显示正常")
            self.add_log("已发送本地测试通知")

        def copy_address(self):
            value = self.addr_var.get()
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.add_log(f"已复制地址: {value}")

        def on_close(self):
            self.server.stop()
            self.root.destroy()

    root = tk.Tk()
    style = ttk.Style(root)
    if 'vista' in style.theme_names():
        style.theme_use('vista')
    App(root)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="NotifySync Windows Server")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--port", type=int, default=PORT, help="服务端口，默认 8787")
    args = parser.parse_args()

    if args.gui:
        run_gui(args.port)
    else:
        run_cli(args.port)


if __name__ == '__main__':
    main()
