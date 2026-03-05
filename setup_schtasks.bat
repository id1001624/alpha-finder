@echo off
setlocal EnableExtensions
chcp 65001 > nul

set "BASE_DIR=C:\Users\w6359\OneDrive\文件\alpha-finder"
set "DAILY_TASK=AlphaFinder_Daily"
set "WEEKLY_TASK=AlphaFinder_WeeklyReview"
set "DAILY_TIME=13:00"
set "WEEKLY_TIME=16:00"
set "WEEKLY_DAY=SUN"

echo 建立/更新工作排程中...

schtasks /create /tn "%DAILY_TASK%" /tr "\"%BASE_DIR%\run_daily.bat\"" /sc daily /st %DAILY_TIME% /it /f
if errorlevel 1 (
    echo [失敗] 建立每日排程失敗，請確認目前使用者有本機建立排程權限。
    exit /b 1
)

schtasks /create /tn "%WEEKLY_TASK%" /tr "\"%BASE_DIR%\run_weekly_review.bat\"" /sc weekly /d %WEEKLY_DAY% /st %WEEKLY_TIME% /it /f
if errorlevel 1 (
    echo [失敗] 建立每週排程失敗，請確認目前使用者有本機建立排程權限。
    exit /b 1
)

echo.
echo [完成] 已建立兩個排程：
echo - %DAILY_TASK%  每日 %DAILY_TIME%
echo - %WEEKLY_TASK% 每週 %WEEKLY_DAY% %WEEKLY_TIME%
echo.
echo 查詢指令：
echo schtasks /query /tn "%DAILY_TASK%" /fo list /v
echo schtasks /query /tn "%WEEKLY_TASK%" /fo list /v
exit /b 0
