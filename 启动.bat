@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DataAcquirer 启动器

REM ---- 检测 Python 解释器 ----
set "PYW="
where pyw >nul 2>nul && set "PYW=pyw"
if not defined PYW where pythonw >nul 2>nul && set "PYW=pythonw"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 及以上版本：
    echo        https://www.python.org/downloads/
    echo        安装时请务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)
if not defined PYW set "PYW=%PY%"

REM ---- 检查依赖，缺失时自动安装 ----
%PY% -c "import influxdb, pandas" 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖，请稍候……
    %PY% -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络或 pip 配置后重试。
        pause
        exit /b 1
    )
)

REM ---- 启动图形界面（不保留终端窗口）----
start "" %PYW% "%~dp0run.py"
exit /b 0
