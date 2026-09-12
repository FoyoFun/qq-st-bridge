@echo off
chcp 65001 >nul
title QQ-ST-Bridge 机器人

echo ============================================
echo   QQ-ST-Bridge 机器人启动器
echo ============================================
echo.

cd /d "%~dp0"

:: 检查是否需要安装依赖
if not exist "requirements.txt" goto :run_bot

echo [*] 检测到 requirements.txt，正在检查依赖...
pip install -r requirements.txt --quiet 2>nul
if %errorlevel% neq 0 (
    echo [!] 依赖安装失败，尝试重新安装...
    pip install -r requirements.txt
)
echo.

:run_bot
echo [*] 正在启动机器人...
echo [*] 按 Ctrl+C 停止
echo.
python bot.py

if %errorlevel% neq 0 (
    echo.
    echo [!] 机器人异常退出 (错误码: %errorlevel%)
    echo.
    pause
    exit /b %errorlevel%
)
