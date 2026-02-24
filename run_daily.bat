@echo off
chcp 65001 > nul
cd /d "C:\Users\w6359\OneDrive\文件\alpha-finder"

REM 設定 SSL 憑證路徑（處理中文路徑問題）
set CURL_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set SSL_CERT_FILE=%USERPROFILE%\.alpha_finder_certs\cacert.pem
set REQUESTS_CA_BUNDLE=%USERPROFILE%\.alpha_finder_certs\cacert.pem

echo [%date% %time%] Alpha Finder 每日掃描開始 >> run_log.txt

"C:\Users\w6359\OneDrive\文件\alpha-finder\.venv\Scripts\python.exe" main.py >> run_log.txt 2>&1

if %errorlevel% == 0 (
    echo [%date% %time%] 掃描完成 OK >> run_log.txt
) else (
    echo [%date% %time%] 掃描失敗，exit code: %errorlevel% >> run_log.txt
)
