@echo off
chcp 65001 >nul
rem lazy-clerk 公网版本地开发启动脚本
cd /d %~dp0

echo ==========================================
echo   lazy-clerk 公网版（本地开发）
echo ==========================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.11+
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx, apscheduler, Crypto, jinja2" >nul 2>&1
if errorlevel 1 (
    echo [提示] 依赖缺失，正在安装...
    pip install -r server\requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

if not exist .env (
    echo [提示] 未找到 .env，已从 server\.env.example 复制一份，请按需修改后重启
    copy server\.env.example .env >nul
)

if not exist data (
    echo [提示] 首次运行，将创建全新数据库 data\lazy-clerk.db
    echo        管理员密码以 .env 中 ADMIN_PASSWORD 为准
)

echo.
echo   用户页:  http://127.0.0.1:8787/login
echo   管理页:  http://127.0.0.1:8787/admin
echo.
echo   已开启代码热重载，修改代码自动重启；Ctrl+C 停止
echo ==========================================
echo.

python -m uvicorn server.main:app --host 127.0.0.1 --port 8787 --reload
pause
