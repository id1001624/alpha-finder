@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "BEDTIME_TASK=AlphaFinder_Discord_Bedtime"
set "MORNING_TASK=AlphaFinder_Discord_Morning"
set "ENGINE_TASK=AlphaFinder_Intraday_Engine"
set "BOT_TASK=AlphaFinder_Discord_TradeBot"
set "ENGINE_STARTUP_FILE=%STARTUP_DIR%\AlphaFinder_Intraday_Engine.cmd"
set "BOT_STARTUP_FILE=%STARTUP_DIR%\AlphaFinder_Discord_TradeBot.cmd"
set "BEDTIME_TIME=22:15"
set "MORNING_TIME=07:15"
set "ENABLE_LOCAL_RECAP_TASKS=false"
set "ENABLE_LOCAL_ENGINE_TASKS=false"
set "ENABLE_LOCAL_BOT_AUTOSTART=false"
set "ENGINE_START_TIME=21:22"
set "ENGINE_REPEAT_MINUTES=5"
set "ENGINE_DURATION=07:50"
set "BOT_DELAY=0001:00"

echo 建立/更新 Alpha Finder 排程中...

if /i "%ENABLE_LOCAL_RECAP_TASKS%" == "true" (
    schtasks /create /tn "%BEDTIME_TASK%" /tr "\"%BASE_DIR%\run_discord_bedtime.bat\"" /sc daily /st %BEDTIME_TIME% /it /f
    if errorlevel 1 (
        echo [失敗] 建立睡前摘要排程失敗。
        exit /b 1
    )

    schtasks /create /tn "%MORNING_TASK%" /tr "\"%BASE_DIR%\run_discord_morning.bat\"" /sc daily /st %MORNING_TIME% /it /f
    if errorlevel 1 (
        echo [失敗] 建立早晨 recap 排程失敗。
        exit /b 1
    )
) else (
    schtasks /delete /tn "%BEDTIME_TASK%" /f > nul 2>&1
    schtasks /delete /tn "%MORNING_TASK%" /f > nul 2>&1
)

if /i "%ENABLE_LOCAL_ENGINE_TASKS%" == "true" (
    schtasks /create /tn "%ENGINE_TASK%" /tr "\"%BASE_DIR%\run_intraday_execution_engine.bat\"" /sc daily /st %ENGINE_START_TIME% /ri %ENGINE_REPEAT_MINUTES% /du %ENGINE_DURATION% /it /f
    if errorlevel 1 (
        echo [失敗] 建立盤中 engine 重複排程失敗。
        exit /b 1
    )
    if exist "%ENGINE_STARTUP_FILE%" del /f /q "%ENGINE_STARTUP_FILE%" > nul 2>&1
) else (
    schtasks /delete /tn "%ENGINE_TASK%" /f > nul 2>&1
    if exist "%ENGINE_STARTUP_FILE%" del /f /q "%ENGINE_STARTUP_FILE%" > nul 2>&1
)

if /i "%ENABLE_LOCAL_BOT_AUTOSTART%" == "true" (
    call :ensure_logon_autostart "%BOT_TASK%" "%BASE_DIR%\run_discord_trade_bot.bat" %BOT_DELAY% "%BOT_STARTUP_FILE%"
    if errorlevel 1 exit /b 1
) else (
    schtasks /delete /tn "%BOT_TASK%" /f > nul 2>&1
    if exist "%BOT_STARTUP_FILE%" del /f /q "%BOT_STARTUP_FILE%" > nul 2>&1
)

echo.
echo [完成] 已同步本機排程與自啟設定：
if /i "%ENABLE_LOCAL_RECAP_TASKS%" == "true" (
    echo - %BEDTIME_TASK% 每日 %BEDTIME_TIME%
    echo - %MORNING_TASK% 每日 %MORNING_TIME%
) else (
    echo - %BEDTIME_TASK% 已停用，改由 GitHub Actions 處理睡前摘要
    echo - %MORNING_TASK% 已停用，改由 GitHub Actions 處理早晨 recap
)
if /i "%ENABLE_LOCAL_ENGINE_TASKS%" == "true" (
    echo - %ENGINE_TASK% 每日 %ENGINE_START_TIME% 開始，每 %ENGINE_REPEAT_MINUTES% 分鐘喚醒一次，持續 %ENGINE_DURATION%
) else (
    echo - %ENGINE_TASK% 已停用，改由 GitHub Actions 處理盤中 engine
)
if /i "%ENABLE_LOCAL_BOT_AUTOSTART%" == "true" (
    echo - %BOT_TASK% 使用者登入後 %BOT_DELAY% 啟動，若排程權限不足則改寫入 Startup
) else (
    echo - %BOT_TASK% 已停用，本機 bot 不再自啟，正式主路徑改由雲端主機 systemd 服務處理
)
echo.
echo 查詢指令：
echo schtasks /query /tn "%BEDTIME_TASK%" /fo list /v
echo schtasks /query /tn "%MORNING_TASK%" /fo list /v
echo schtasks /query /tn "%ENGINE_TASK%" /fo list /v
echo schtasks /query /tn "%BOT_TASK%" /fo list /v
echo Startup 資料夾：
echo %STARTUP_DIR%
exit /b 0

:ensure_logon_autostart
set "TASK_NAME=%~1"
set "TASK_TARGET=%~2"
set "TASK_DELAY=%~3"
set "STARTUP_FILE=%~4"

schtasks /create /tn "%TASK_NAME%" /tr "\"%TASK_TARGET%\"" /sc onlogon /delay %TASK_DELAY% /it /f > nul 2>&1
if errorlevel 1 (
    echo [警告] 建立 %TASK_NAME% 的 onlogon 排程失敗，改用 Startup 自啟。
    call :write_startup_launcher "%STARTUP_FILE%" "%TASK_TARGET%"
    if errorlevel 1 exit /b 1
    exit /b 0
)

echo [完成] %TASK_NAME% 已建立為登入自啟排程。
exit /b 0

:write_startup_launcher
set "STARTUP_FILE=%~1"
set "TARGET_FILE=%~2"

if not exist "%STARTUP_DIR%" mkdir "%STARTUP_DIR%"
(
    echo @echo off
    echo call "%TARGET_FILE%"
) > "%STARTUP_FILE%"
if not exist "%STARTUP_FILE%" (
    echo [失敗] 寫入 Startup 啟動檔失敗：%STARTUP_FILE%
    exit /b 1
)

echo [完成] 已寫入 Startup 啟動檔：%STARTUP_FILE%
exit /b 0