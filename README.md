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

如果你是走 Dropbox 給 AI 讀 md 檔，而不是直接上傳 xlsx：

```powershell
python .\scripts\upload_ai_ready_to_dropbox.py
```

這支腳本會把 repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx 轉成給 AI 使用的 12 個核心 md，輸出到 repo_outputs/ai_ready/latest/ai_ready_bundle_md/，並用 DROPBOX_APP_KEY、DROPBOX_APP_SECRET、DROPBOX_REFRESH_TOKEN 自動刷新 access token 後上傳。

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
- /buy
- /add
- /sell

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

- 預設只在本機時間 21:20 到 05:10 之間跑 loop
- 超出時段會自動 idle，不抓資料、不推 Discord
- 預設每 5 分鐘輪詢一次，超出時段則改成較低頻率待命

所以 README 不再列這些手動執行指令，避免你混淆。

如果你改了 token、頻道 ID 或其他設定，只要重跑一次：

```powershell
.\setup.bat
```

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
- Windows 自啟與排程已完成

下一階段若還要做，只會是優化項，不是基礎缺口。
