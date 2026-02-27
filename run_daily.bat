@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "LOG_FILE=%BASE_DIR%\run_log.txt"
set "LOCK_FILE=%BASE_DIR%\.run_daily.lock"
set "PYTHON_EXE=%BASE_DIR%\.venv\Scripts\python.exe"

cd /d "%BASE_DIR%" || (
    echo [%date% %time%] 啟動失敗：找不到專案目錄 %BASE_DIR% >> "%LOG_FILE%"
    exit /b 2
)

if exist "%LOCK_FILE%" (
    echo [%date% %time%] 偵測到執行鎖，略過本次（避免重複執行） >> "%LOG_FILE%"
    exit /b 0
)

echo %date% %time% > "%LOCK_FILE%"

if not exist "%PYTHON_EXE%" (
    echo [%date% %time%] 啟動失敗：找不到 Python %PYTHON_EXE% >> "%LOG_FILE%"
    del /f /q "%LOCK_FILE%" > nul 2>&1
    exit /b 3
)

REM 設定 SSL 憑證路徑（處理中文路徑問題）
set CURL_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set SSL_CERT_FILE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set REQUESTS_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%date% %time%] Alpha Finder 每日掃描開始 >> "%LOG_FILE%"

"%PYTHON_EXE%" main.py >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%errorlevel%"

if "%EXIT_CODE%" == "0" (
    echo [%date% %time%] 掃描完成 OK >> "%LOG_FILE%"
) else (
    echo [%date% %time%] 掃描失敗，exit code: %EXIT_CODE% >> "%LOG_FILE%"
)

del /f /q "%LOCK_FILE%" > nul 2>&1
exit /b %EXIT_CODE%
