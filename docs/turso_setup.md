# Turso 設定說明

Alpha Finder 現在支援把最新 runtime state 寫到 Turso，讓 GitHub Actions 與未來雲端 Discord Bot 直接讀取，不再依賴 repo 內的中介狀態檔。

## 目前會同步哪些資料

- ai_decision_latest
- positions_latest
- execution_trade_latest
- execution_trade_log
- position_trade_log

注意：目前仍然不搬整包 backtest 歷史，但 Discord bot 的成交 ledger 與 execution_trade_log 已納入 Turso，至少成交與盤中 execution 不會只留在本機。

## 你需要在 Turso 做什麼

1. 到 <https://turso.tech> 註冊帳號
2. 建立一個 database，例如 alpha-finder-state
3. 在 Turso 建立一個 auth token
4. 記下兩個值：
   - TURSO_DATABASE_URL
   - TURSO_AUTH_TOKEN

## 本機環境變數

至少要設定這三個環境變數：

```powershell
$env:TURSO_ENABLED="true"
$env:TURSO_DATABASE_URL="libsql://your-database.turso.io"
$env:TURSO_AUTH_TOKEN="your-token"
```

如果要設成 Windows 使用者永久環境變數：

```powershell
[Environment]::SetEnvironmentVariable("TURSO_ENABLED", "true", "User")
[Environment]::SetEnvironmentVariable("TURSO_DATABASE_URL", "libsql://your-database.turso.io", "User")
[Environment]::SetEnvironmentVariable("TURSO_AUTH_TOKEN", "your-token", "User")
```

## GitHub Secrets

請在 GitHub repository secrets 新增：

- TURSO_DATABASE_URL
- TURSO_AUTH_TOKEN

workflow 內已預留這兩個 env；設完之後，雲端 recap 與 intraday monitor 就會優先讀 Turso。

## 初始化同步

裝完依賴後可手動跑一次：

```powershell
python .\scripts\sync_turso_state.py
```

這會把目前本機可取得的 latest state 推到 Turso。
也會把 `position_trade_log.csv` 與 `execution_trade_log.csv` 一併匯入 Turso。

## 讀寫優先順序

目前程式的 latest state 讀取順序是：

1. Turso
2. repo_outputs/backtest 下的本機 CSV

寫入則是：

1. 先照舊寫本機 CSV
2. 若 Turso 已設定完成，再同步到 Turso

## 目前還沒搬過去的東西

- ai_decision_log.csv 全歷史
- alert_log.csv

如果下一步要完全去本機，再把 Discord bot 的持久化與查詢主來源也完全改到 Turso 即可。
