# Alpha Finder 個人操作手冊

## 現在這個專案是什麼

Alpha Finder 現在已經是一條完整的研究到執行追蹤流程。

- 每天先跑主產線找出候選股
- Web 或 API 產出 ai_decision_YYYY-MM-DD.csv
- 系統把決策歸檔到 backtest
- 盤中由 repo 內建 engine 自己計算分鐘級訊號
- Discord Bot 負責接你回報的真實成交
- 系統接續管理持倉與提醒

這套系統不自動下單。

## 你平常怎麼操作

你平常真的只要做這 4 步。

1. 跑主產線

```powershell
.\run_daily.bat
```

1. 把這 2 個檔案丟給網頁 AI

- repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx
- Alpha-Sniper-Protocol.md

1. 把網頁 AI 輸出的 ai_decision_YYYY-MM-DD.csv 放進 repo_outputs/backtest/inbox/

1. 歸檔決策

```powershell
python .\scripts\record_ai_decision.py --auto-latest
```

做完後，盤中提醒與 Discord 回報成交就是這條線的後半段。

## Discord 怎麼用

你現在的 Discord Bot 已經是正式操作面。

平常你只要用 slash commands：

- /tradehelp
- /positions
- /position
- /trades
- /executions
- /buy
- /add
- /sell

這兩個新查詢指令的用途：

- `/trades`: 查你透過 Discord 回報過的真實成交紀錄，來源是 Turso 的 `position_trade_log`
- `/executions`: 查 engine 或 TradingView execution 流程產生的執行歷史，來源是 Turso 的 `execution_trade_log`

推薦實際用法：

1. 先看目前部位

```text
/positions
```

1. 有真實成交時回報

```text
/buy
/add
/sell
```

1. 系統之後會根據目前持倉與盤中訊號繼續提醒你

補充：

- !buy、!sell 這類文字指令仍然能用，但平常直接用 slash commands 就好
- 若你只是做測試單，建議用一筆 /sell 把測試倉位平掉，不要直接手改 csv

## 盤中系統現在怎麼運作

現在盤中 execution 已經不是靠你手動維護 TradingView alert。

- watchlist 來源是 ai_decision_latest.csv
- engine 自己抓分鐘級 OHLCV
- 自己算 Dynamic AVWAP + SQZMOM
- 只輸出四種執行建議：適合買、可加碼、適合先賣一部分、適合全出
- 你在 Discord 回報真實成交後，系統用該成交更新持倉與後續提醒

分鐘資料源：

- 預設 INTRADAY_DATA_PROVIDER=auto
- 有 FINNHUB_API_KEY 時優先走 Finnhub 免費分鐘行情
- 否則自動 fallback 到 yfinance

盤中執行時段：

- 預設 active window 是本機時間 21:20 到 05:10
- 超出時段會自動略過，不抓資料、不推 Discord
- 盤中 engine 預設每 5 分鐘喚醒一次、跑完就退出

現在的正式主流程是 GitHub Actions 雲端排程加上 Turso 狀態儲存，不是本機夜間排程：

- repo 內有獨立的 intraday monitor workflow
- 也有對應的 bedtime recap 與 morning recap workflows
- 排程採用偏移分鐘，不用整點附近的 0,5,10
- workflow 會跑在 default branch 最新 commit，不會讀你本機未提交檔案
- GitHub Actions 與 Discord bot 現在都優先讀 Turso
- 目前同步目標是 `ai_decision_latest.csv`、`positions_latest.csv`、`execution_trade_latest.csv`、`position_trade_log.csv`、`execution_trade_log.csv`
- 若已設定 `TURSO_ENABLED=true`、`TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`，最新決策、持倉、成交與 execution 歷史都會同步到 Turso
- 本機 CSV 現在主要保留給 backtest、人工檢查與最後備援，不再需要 `cloud_state/`
- Turso 註冊與設定步驟看 `docs/turso_setup.md`
- Discord bot 雲端主機部署步驟看 `docs/discord_bot_cloud_host.md`
- Discord 交易 bot 本身仍然是常駐型服務，GitHub Actions 不能取代它的 slash command/gateway 連線

本機排程現在只視為備援，不是正式主路徑：

- 如果你重跑 `setup.bat`，預設不會再建立本機 intraday engine 夜間排程
- 本機 22:15 與 07:15 recap 排程也預設停用
- 只有 Discord bot 仍可能以本機自啟方式存在，因為 slash commands 需要一個常駐 bot process

本機 recap 排程：

- 既然雲端 recap 已上線，本機 22:15 與 07:15 排程可以刪除，避免重複發送
- `setup.bat` 現在預設會停用這兩個本機 recap 排程
- 若你真的想保留本機備援，再把 `setup.bat` 內的 `ENABLE_LOCAL_RECAP_TASKS` 改成 `true`

所以 README 不再列這些手動執行指令，避免你混淆。

如果你改了 token、頻道 ID 或其他設定，只要重跑一次：

```powershell
.\setup.bat
```

你現在晚上是否需要再開著電腦：

- `engine` 與 `bedtime/morning recap` 不需要，本機關機也沒關係
- `Discord bot` 如果還是跑在你這台電腦上，那台電腦關掉時 bot 就不在線

## 關鍵輸出檔

你平常最常看的檔案只有這些：

- 決策主檔：repo_outputs/backtest/ai_decision_log.csv
- 最新決策：repo_outputs/backtest/ai_decision_latest.csv
- 開倉部位：repo_outputs/backtest/positions_latest.csv
- 真實成交紀錄：repo_outputs/backtest/position_trade_log.csv
- 盤中快照：repo_outputs/backtest/intraday/intraday_signal_latest.csv
- 執行主檔：repo_outputs/backtest/execution_trade_log.csv

## Web / API 模式

預設是 AI_RESEARCH_MODE='web'。

- web：你把 bundle 丟給網頁 AI，然後把回傳的 ai_decision_YYYY-MM-DD.csv 放進 inbox
- api：系統自己走 Tavily + Gemini 備援流程

兩種模式最後都要回到同一份決策契約：

- ai_decision_YYYY-MM-DD.csv

## 每週回看什麼

每週回看這幾個檔案就夠：

- repo_outputs/backtest/ai_decision_log.csv
- repo_outputs/backtest/execution_trade_log.csv
- repo_outputs/backtest/weekly_reports/weekly_report_latest.md

## 專案現況

就目前目標來說，這個專案已經完成可用版本。

現在的狀態不是「開發中 demo」，而是：

- daily 研究流程可跑
- AI 決策契約穩定
- XQ 新鮮度防呆已完成
- 盤中分鐘級訊號引擎已完成
- Discord Bot 回報成交已完成
- engine 與 recap 雲端排程已完成
- Discord Bot 仍是常駐服務，尚未完全雲端化

下一階段若還要做，只會是優化項，不是基礎缺口。
