#!/usr/bin/env python3
"""
NotifySync EXE 打包脚本
"""
import PyInstaller.__main__
import os

# 确保资源文件路径正确
script_dir = os.path.dirname(__file__)
script_path = os.path.join(script_dir, 'notify_server_tray.py')


def pick_icon(base_dir: str):
    candidates = []

    # 0) 优先使用你指定的图标文件
    preferred_icon = os.path.join(base_dir, 'icon', 'All-Platforms-Icons (1)', 'notifysync.ico')
    if os.path.exists(preferred_icon):
        return preferred_icon

    # 1) icon 子目录内的 ico（递归）
    icon_dir = os.path.join(base_dir, 'icon')
    if os.path.isdir(icon_dir):
        for root, _, files in os.walk(icon_dir):
            for f in files:
                if f.lower().endswith('.ico'):
                    p = os.path.join(root, f)
                    candidates.append((os.path.getmtime(p), p))

    # 2) 根目录常见命名回退
    for name in ('notifysync.ico', 'app.ico', 'icon.ico', 'logo.ico'):
        p = os.path.join(base_dir, name)
        if os.path.exists(p):
            candidates.append((os.path.getmtime(p), p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


icon_path = pick_icon(script_dir)

# 某些环境下带空格/括号的绝对路径可能导致 PyInstaller 图标参数识别异常，
# 这里复制到当前目录固定文件名后再使用。
staged_icon_path = None
if icon_path and os.path.exists(icon_path):
    staged_icon_path = os.path.join(script_dir, 'notifysync.ico')
    try:
        if os.path.abspath(icon_path) != os.path.abspath(staged_icon_path):
            import shutil
            shutil.copy2(icon_path, staged_icon_path)
        else:
            staged_icon_path = icon_path
    except Exception as e:
        print(f"复制图标失败，回退原路径: {e}")
        staged_icon_path = icon_path

# 确保传给 PyInstaller 的是“真实 ICO”（有些文件虽然后缀是 .ico，实际是 png）
final_icon_path = None
if staged_icon_path and os.path.exists(staged_icon_path):
    try:
        from PIL import Image
        with Image.open(staged_icon_path) as img:
            # 强制转成标准多尺寸 ICO
            generated_icon = os.path.join(script_dir, 'notifysync.generated.ico')
            img = img.convert('RGBA')
            sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
            img.save(generated_icon, format='ICO', sizes=sizes)
            final_icon_path = generated_icon
            print(f"图标已标准化为 ICO: {final_icon_path}")
    except Exception as e:
        print(f"图标标准化失败，使用原图标: {e}")
        final_icon_path = staged_icon_path

icon_arg = f"--icon={final_icon_path}" if final_icon_path else '--icon=NONE'
if final_icon_path:
    print(f"使用 EXE 图标: {final_icon_path} (来源: {icon_path})")
else:
    print("未找到 ico，EXE 将使用默认图标")

add_data_args = [
    '--add-data=requirements.txt;.',
]

# 把当前选中的图标也打进包内，确保运行时窗口/托盘可加载
if icon_path and os.path.exists(icon_path):
    add_data_args.append(f'--add-data={icon_path};.')

for name in ('notifysync.png', 'notifysync.ico'):
    p = os.path.join(script_dir, name)
    if os.path.exists(p):
        add_data_args.append(f'--add-data={p};.')

PyInstaller.__main__.run([
    script_path,
    '--name=NotifySync',
    '--onefile',  # 打包成单个exe
    '--windowed',  # 不显示控制台窗口
    icon_arg,
    *add_data_args,
    '--hidden-import=win11toast',
    '--hidden-import=pystray',
    '--hidden-import=PIL',
    '--hidden-import=PIL._tkinter_finder',
    '--hidden-import=tkinter',
    '--hidden-import=winreg',
    '--hidden-import=Crypto',
    '--hidden-import=Crypto.Cipher',
    '--hidden-import=Crypto.Cipher.AES',
    '--hidden-import=Cryptodome',
    '--hidden-import=Cryptodome.Cipher',
    '--hidden-import=Cryptodome.Cipher.AES',
    '--clean',
    '--noconfirm',
])

print("打包完成！exe文件在 dist/NotifySync.exe")
