@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "PYTHON_EXE=%BASE_DIR%\.venv\Scripts\python.exe"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=web"

cd /d "%BASE_DIR%" || (
    echo [COMPARE] 啟動失敗：找不到專案目錄 %BASE_DIR%
    exit /b 2
)

if not exist "%PYTHON_EXE%" (
    echo [COMPARE] 啟動失敗：找不到 Python %PYTHON_EXE%
    exit /b 3
)

if /I "%MODE%"=="api" (
    echo [COMPARE] mode=api（會跑 balanced + monster_v1）
    "%PYTHON_EXE%" scripts\run_ai_profile_compare.py --research-mode api --enable-catalyst
) else (
    echo [COMPARE] mode=web（會跑 balanced + monster_v1）
    "%PYTHON_EXE%" scripts\run_ai_profile_compare.py --research-mode web
)

exit /b %errorlevel%
