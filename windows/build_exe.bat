@echo off
chcp 65001 >nul
title Build NotifySync EXE

echo ========================================
echo   Build NotifySync EXE
echo ========================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

echo 安装/检查依赖...
pip install -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败
    pause
    exit /b 1
)

echo 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist NotifySync.spec del /q NotifySync.spec

echo 开始打包 EXE...
python build_exe.py
if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

echo.
echo 打包成功！
echo EXE 路径: %cd%\dist\NotifySync.exe
echo.
pause
