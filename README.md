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

如果同一天你讓 AI 重生一份新版 ai_decision，想用新版整份取代舊版，改用：

```powershell
python .\scripts\record_ai_decision.py --auto-latest --replace-date
```

這會先把 ai_decision_log.csv 中同一個 decision_date 的舊列刪掉，再寫入新版，避免殘留舊 ticker。

做完後，盤中提醒與 Discord 回報成交就是這條線的後半段。

## Discord 怎麼用

你現在的 Discord Bot 已經是正式操作面。

平常你只要用 slash commands：

- /tradehelp
- /positions
- /position
- /trades
- /executions
- /watchlist
- /watchadd
- /watchremove
- /watchsaved
- /buy
- /add
- /sell

這兩個新查詢指令的用途：

- `/trades`: 查你透過 Discord 回報過的真實成交紀錄，來源是 Turso 的 `position_trade_log`
- `/executions`: 查 engine 或 TradingView execution 流程產生的執行歷史，來源是 Turso 的 `execution_trade_log`
- `/watchlist`: 把最新 ai_decision、你目前持倉、以及你保存或臨時輸入的關注股一起比較，只回傳最終排序、風險先處理與現在先做
- `/watchadd`: 把 ticker 加進你自己的保存關注股
- `/watchremove`: 從你的保存關注股移除 ticker
- `/watchsaved`: 看你目前保存的關注股清單

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

如果你想建立自己的保存關注股，直接：

```text
/watchadd AAPL NVDA TSLA
/watchsaved
```

如果你想在當下再額外塞幾檔進去一起比，也可以直接：

```text
/watchlist AAPL NVDA TSLA
```

它會把這些 ticker 跟最新 ai_decision、目前持倉一起比較，只給你最終結果：

- 哪些最值得優先看
- 哪些先處理風險
- 現在先做什麼

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
- 若該輪沒有新的 entry/add/take profit/stop loss 訊號，現在也會每 30 分鐘送一則 heartbeat，表示監控仍在線

現在的正式主流程是 GitHub Actions 雲端排程加上 Turso 狀態儲存，不是本機夜間排程：

- repo 內有獨立的 intraday monitor workflow
- 也有對應的 bedtime recap、opening recap 與 morning recap workflows
- 排程採用偏移分鐘，不用整點附近的 0,5,10
- workflow 會跑在 default branch 最新 commit，不會讀你本機未提交檔案
- GitHub Actions 與 Discord bot 現在都優先讀 Turso
- 目前同步目標是 `ai_decision_latest.csv`、`positions_latest.csv`、`execution_trade_latest.csv`、`position_trade_log.csv`、`execution_trade_log.csv`
- 若已設定 `TURSO_ENABLED=true`、`TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`，最新決策、持倉、成交與 execution 歷史都會同步到 Turso
- 本機 CSV 現在主要保留給 backtest、人工檢查與最後備援，不再需要 `cloud_state/`
- Turso 註冊與設定步驟看 `docs/turso_setup.md`
- Discord bot 雲端主機部署步驟看 `docs/discord_bot_cloud_host.md`
- 更新雲端 bot 程式時，直接執行 `deploy\redeploy_discord_bot.ps1`
- Discord 交易 bot 本身仍然是常駐型服務，GitHub Actions 不能取代它的 slash command/gateway 連線

監控 heartbeat 補充：

- heartbeat 預設已開啟
- 預設每 30 分鐘最多送一則
- 只有在該輪沒有新的盤中執行建議時才送 heartbeat，避免和真正告警混在一起刷版
- 可用環境變數調整：`INTRADAY_HEARTBEAT_ENABLED=true|false`、`INTRADAY_HEARTBEAT_INTERVAL_MINUTES=30`

如果你改了 token、頻道 ID 或其他設定，只要重跑一次：

```powershell
.\setup.bat
```

更新 bot 是什麼意思：

- 指把 repo 目前最新的 bot 程式重新部署到 Oracle Cloud VM
- 內容包含：上傳最新程式、必要時重裝依賴、重啟 `alpha-finder-discord-bot.service`
- 這個腳本部署的是目前 git `HEAD`；如果你要把最新修改一起帶上去，先 commit
- 若你有改 bot 程式或 README 旁邊提到的部署資產，就用 `powershell -ExecutionPolicy Bypass -File .\deploy\redeploy_discord_bot.ps1`
- 若你連環境變數也一起改了，再加上 `-SyncEnv`

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

你如果現在就在這個路徑：

```powershell
(.venv) PS C:\Users\w6359\OneDrive\文件\alpha-finder>
```

直接用這個指令切換：

- 切到 web：`./switch_ai_research_mode.ps1 web`
- 切到 api：`./switch_ai_research_mode.ps1 api`
- 查看目前模式：`./switch_ai_research_mode.ps1 status`

這個腳本會同時處理：

- `AI_RESEARCH_MODE`
- `CATALYST_DETECTOR_ENABLED`

也就是說：

- `web` 會設成 `AI_RESEARCH_MODE=web` 並關掉 `CATALYST_DETECTOR_ENABLED`
- `api` 會設成 `AI_RESEARCH_MODE=api` 並打開 `CATALYST_DETECTOR_ENABLED`

切完後直接跑：

```powershell
.\run_daily.bat
```

就會用你剛切好的模式往下跑。

- web：你把 bundle 丟給網頁 AI，然後把回傳的 ai_decision_YYYY-MM-DD.csv 放進 inbox
- api：系統自己走 Tavily + Gemini 備援流程

如果 web AI 第一次輸出有錯、你又重生同一天的新版本，第二次歸檔建議加 `--replace-date`，把當天整份決策完整覆蓋。

兩種模式最後都要回到同一份決策契約：

- ai_decision_YYYY-MM-DD.csv

## 每週回看什麼

每週回看這幾個檔案就夠：

- repo_outputs/backtest/ai_decision_log.csv
- repo_outputs/backtest/execution_trade_log.csv
- repo_outputs/backtest/weekly_reports/weekly_report_latest.md

## 怎麼測通知正常

你要驗證盤中監控能不能通知，可以用這兩步：

1. 先看本機 dry-run 有沒有算出訊號

```powershell
c:/Users/w6359/OneDrive/文件/alpha-finder/.venv/Scripts/python.exe .\scripts\run_intraday_execution_engine.py --top-n 3 --dry-run
```

1. 再到 GitHub Actions 手動觸發 `Intraday Monitor`

- 如果有新的 action signal，Discord 會收到真正的 engine 告警
- 如果沒有新的 action signal，但 heartbeat 到期，Discord 會收到 heartbeat
- workflow log 內現在也會印出 `[DISCORD] ok=True detail=...`，可直接確認是否送出成功

## 專案現況

就目前目標來說，這個專案已經完成可用版本。

現在的狀態不是「開發中 demo」，而是：

- daily 研究流程可跑
- AI 決策契約穩定
- XQ 新鮮度防呆已完成
- 盤中分鐘級訊號引擎已完成
- Discord Bot 回報成交已完成
- engine 與 recap 雲端排程已完成
- Discord Bot 已雲端化並由 Oracle Cloud systemd 常駐

所以目前已經是可線上完成追蹤、查詢、回報與持倉同步的完整版本。

下一階段若還要做，只會是優化項，不是基礎缺口。
