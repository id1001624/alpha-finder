@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "PYTHON_EXE=%BASE_DIR%\.venv\Scripts\python.exe"

cd /d "%BASE_DIR%" || exit /b 2

"%PYTHON_EXE%" scripts\sync_cloud_state.py
if errorlevel 1 exit /b %errorlevel%

git add cloud_state
git diff --cached --quiet -- cloud_state
if not errorlevel 1 (
    echo cloud_state 沒有變更，不需要 commit。
    exit /b 0
)

git commit -m "chore(cloud_state): 同步最新雲端狀態"
if errorlevel 1 exit /b %errorlevel%

git push origin main
exit /b %errorlevel%