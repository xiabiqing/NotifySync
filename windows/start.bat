@echo off
chcp 65001 >nul
title NotifySync 小白版
echo ========================================
echo   NotifySync Windows GUI
echo ========================================
echo.

:: 切换到当前脚本目录
cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

:: 检查并安装依赖
echo 检查依赖...
python -c "import win11toast" >nul 2>&1
if errorlevel 1 (
    echo 正在安装依赖，请稍候...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络或 pip 配置
        pause
        exit /b 1
    )
)

echo 启动图形界面...
python notify_server.py --gui

pause
