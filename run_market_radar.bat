@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "LOG_FILE=%BASE_DIR%\run_log.txt"
set "PYTHON_EXE=%BASE_DIR%\.venv\Scripts\python.exe"

cd /d "%BASE_DIR%" || (
    echo [%date% %time%] Radar 啟動失敗：找不到專案目錄 %BASE_DIR% >> "%LOG_FILE%"
    exit /b 2
)

if not exist "%PYTHON_EXE%" (
    echo [%date% %time%] Radar 啟動失敗：找不到 Python %PYTHON_EXE% >> "%LOG_FILE%"
    exit /b 3
)

set CURL_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set SSL_CERT_FILE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set REQUESTS_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%date% %time%] Layer-1 radar 開始（XQ + Finviz） >> "%LOG_FILE%"

"%PYTHON_EXE%" scripts\update_xq_with_history.py >> "%LOG_FILE%" 2>&1
set "XQ_EXIT=%errorlevel%"
if not "%XQ_EXIT%" == "0" (
    echo [%date% %time%] Layer-1 radar: XQ 更新失敗，exit code %XQ_EXIT% >> "%LOG_FILE%"
    exit /b %XQ_EXIT%
)

"%PYTHON_EXE%" scripts\finviz_momentum_scanner.py >> "%LOG_FILE%" 2>&1
set "FINVIZ_EXIT=%errorlevel%"
if not "%FINVIZ_EXIT%" == "0" (
    echo [%date% %time%] Layer-1 radar: Finviz 掃描失敗，exit code %FINVIZ_EXIT% >> "%LOG_FILE%"
    exit /b %FINVIZ_EXIT%
)

echo [%date% %time%] Layer-1 radar 完成 >> "%LOG_FILE%"
exit /b 0
