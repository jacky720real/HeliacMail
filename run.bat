@echo off
chcp 65001 >nul
title HeliacMail · 多邮箱 AI 助手
cd /d "%~dp0"

echo ================================
echo   HeliacMail · 多邮箱 AI 助手 v2 一键启动
echo   (邮件 AI 分析 + 待办 + ntfy 推送)
echo ================================
echo.

:: 选择 Python
where py >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python 启动器 py
    echo 请先安装 Python 3.9+ : https://www.python.org/downloads/
    echo 安装时勾选 Add python.exe to PATH
    pause
    exit /b 1
)

:: 首次运行自动安装依赖
py -c "import requests, yaml, psutil" >nul 2>nul
if errorlevel 1 (
    echo [首次运行] 正在安装依赖，请稍候...
    py -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

echo [启动程序] （账户配置会保留，不会每次清空）
echo.
echo Web 界面: http://localhost:8090  （按 Ctrl+C 停止）
echo.
py app.py
echo.
echo 程序已退出.
pause
