@echo off
chcp 65001 >nul
echo ========================================
echo   NotifySync EXE 打包工具
echo ========================================
echo.

cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python
    pause
    exit /b 1
)

:: 安装依赖
echo 安装打包依赖...
pip install pyinstaller pystray pillow win11toast -q
if errorlevel 1 (
    echo 安装失败，请检查网络
    pause
    exit /b 1
)

:: 打包
echo.
echo 开始打包...这可能需要几分钟
echo.
python build_exe.py

if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   打包成功！
echo   文件位置: dist\NotifySync.exe
echo ========================================
pause
