@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "PYTHON_EXE=%BASE_DIR%\.venv\Scripts\python.exe"
set "LOG_FILE=%BASE_DIR%\run_log.txt"

cd /d "%BASE_DIR%" || exit /b 2

echo [%date% %time%] 步驟：Repo intraday execution engine 開始 >> "%LOG_FILE%"
"%PYTHON_EXE%" scripts\run_intraday_execution_engine.py --respect-active-window >> "%LOG_FILE%" 2>&1
exit /b %errorlevel%