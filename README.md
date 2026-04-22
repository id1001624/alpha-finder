# Alpha Finder — 無腦投資操作版

你平常只做 2 件事：

1. 每天產出最新決策
2. 有真實成交時立刻回報 Discord

盤中監控、風控提醒、Swing 掃描都維持自動。
Recap（睡前 / 早晨 / 開盤 / watchlist）改成你用 Discord 指令手動觸發，避免排程延遲。

如果你現在只想先搞懂怎麼用，先看這兩份：

- [docs/DAILY_SOP.md](docs/DAILY_SOP.md)
- [docs/V3.1_SPEC.md](docs/V3.1_SPEC.md)

## 每天怎麼操作

### 1. 先跑 Layer-1 市場雷達（XQ + Finviz）

```powershell
.\run_market_radar.bat
```

這步只會更新外部候選來源，不會直接改變 bundle 主排序。

### 2. 跑主產線（Layer-2 判讀）

```powershell
.\run_daily.bat
```

補充：主產線不再執行 XQ/Finviz 掃描，僅做候選判讀與 bundle 輸出。

補充：主產線若產出 `ai_decision_seed_YYYY-MM-DD.csv` 或 `protocol_release_preview_YYYY-MM-DD.csv`，那是 preview/fallback，不是官方最終決策。

### 3. 把這 2 個檔案丟給網頁 AI

- repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx
- repo_outputs/ai_ready/latest/Alpha-Sniper-Protocol.md

### 4. 把 AI 回傳的決策檔放回 repo

把 ai_decision_YYYY-MM-DD.csv 放進：

- repo_outputs/backtest/inbox/

注意：只有你從 Web AI 產出的 `ai_decision_YYYY-MM-DD.csv` 才是 official final，pipeline 產生的 preview 不可直接當 final 歸檔。

### 5. 歸檔決策

```powershell
python .\scripts\record_ai_decision.py --auto-latest
```

如果同一天你重做了一份新版決策，想整份覆蓋舊版：

```powershell
python .\scripts\record_ai_decision.py --auto-latest --replace-date
```

做完這 4 步，接下來只要看 Discord。

補充（資料層驗證）：

- `repo_outputs/ai_trading/latest/bundle_contract_status.csv`：顯示 `ERROR_LOCAL_CORE_MISSING` / `ERROR_SNIPER_PIPELINE_MISSING` 等契約碼。
- `repo_outputs/ai_trading/latest/pre_event_watchlist.csv`、`live_event_feed.csv`、`event_score_log.csv`、`trade_trigger_queue.csv`：sniper lane 的資料層輸出，提供 Web AI 最終判讀。

如果有買槓桿請在config.py:474 的 LEVERAGED_ETF_MAP 加入對應映射。

欄位契約延伸說明（final_impact / swing_strategy_recommendation / similar_past_trades）：

- docs/ai_decision_contract_extensions.md

## 回測要看什麼

平常先看這 4 個：

- repo_outputs/backtest/ai_decision_log.csv
  你每天最後採用的 AI 決策紀錄。

- repo_outputs/backtest/position_trade_log.csv
  你真實成交的主紀錄。要看自己到底賺賠，先看這個。

- repo_outputs/backtest/execution_trade_log.csv
  系統每次 entry、add、take_profit、stop_loss、swing_entry、swing_exit 的紀錄。

- repo_outputs/backtest/weekly_reports/weekly_report_latest.md
  每週制度化報告，適合快速看整週結果。

如果你想看更像儀表板的整理，再跑一次：

```powershell
python .\scripts\generate_backtest_metrics.py
```

它會產出：

- repo_outputs/backtest/metrics_dashboard_latest.md

## 哪些檔案丟給我最有用

如果你想叫我幫你檢討策略、找問題、看哪裡該改，優先丟這些：

1. repo_outputs/backtest/position_trade_log.csv
2. repo_outputs/backtest/execution_trade_log.csv
3. repo_outputs/backtest/weekly_reports/weekly_report_latest.md
4. repo_outputs/backtest/metrics_dashboard_latest.md
5. repo_outputs/backtest/ai_decision_log.csv

你如果只想問「為什麼 bot 這樣提醒」或「這筆為什麼停損」，通常前 3 個就夠了。

## QuantMuse 狀態確認

如果 ai_decision_log.csv 裡 decision_tag 全部是 rule_based，
代表 QuantMuse 掉回 fallback，執行以下指令確認：
sudo systemctl status alpha-finder-discord-bot --no-pager
tail -50 /var/log/alpha-finder/discord-bot.log | grep -i "has_langchain\|quantmuse"

## Discord 指令

說明：

- 沒有 [] 的參數 = 必填
- 有 [] 的參數 = 可不填

### 查詢類

- /tradehelp
  看所有可用指令與範例。

- /positions
  看目前全部開倉部位。

- /position ticker
  看單一股票目前持倉。ticker 就是股票代號，例如 MU、AAPL。

- /trades [ticker] [limit]
  看你回報過的真實成交紀錄。
  ticker 可不填；不填就是全部股票。limit 是要看幾筆，預設 5，最大 20。

- /executions [ticker] [limit]
  看系統 execution 歷史，包含 engine 與 TradingView 執行訊號。
  ticker 可不填；不填就是全部股票。limit 是要看幾筆，預設 5，最大 20。

### 成交回報類

- /buy ticker quantity price [note] [profile]
  回報新買進成交。ticker 是股票代號，quantity 是股數，price 是你的真實成交價。
  note 可留空。profile 不填時，預設是 monster。

- /add ticker quantity price [note] [profile]
  回報加碼成交。price 一樣要填真實成交價。
  note 可留空。profile 不填時，預設是 monster。

- /sell ticker quantity price [note] [profile]
  回報賣出成交。price 要填你的真實成交價，系統才會算對損益。
  note 可留空。profile 不填時，預設是 monster。

profile 要怎麼選：

- monster：這筆是盤中策略單。
- swing：這筆是多日策略單。

這個很重要：

- 在 watchsaved 裡，不代表 /buy 會自動變 swing。
- 你要買成 swing 倉，就要在 /buy、/add、/sell 明確選 swing。
- 如果你沒填 profile，系統會直接當成 monster。
- 同一筆倉位之後的 /add、/sell，也要沿用同一個 profile，不要混用。

### Watchlist 類

- /watchlist [tickers]
  輸出樣式已併入 /recap watchlist（同一種結論卡格式）。
  把最新 ai_decision、你的持倉、保存關注股、臨時輸入股票整合成結論卡。
  tickers 可不填；如果要填，就是臨時多加幾檔一起比較。

- /watchadd tickers
  把股票加入你自己的保存關注清單。
  可以一次加多檔，空白或逗號分隔都可以，例如：AAPL NVDA TSLA。

- /watchremove tickers
  從保存關注清單移除股票。
  可以一次刪多檔，空白或逗號分隔都可以。

- /watchsaved
  看你目前保存的關注股。

- /recap [mode] [debug] [tickers]
  手動觸發 recap。
  mode 可選 bedtime / morning / opening / watchlist。
  debug 可選 true/false；true 時會附上 Gemini/Tavily 啟用狀態與新聞覆蓋數。
  tickers 只在 mode=watchlist 有效，可臨時加股票一起看。

- /recapstatus
  顯示最近一次 recap 的命中摘要與原因碼。
  這個指令不會觸發 Gemini/Tavily API，只讀最新狀態檔。

## 參數到底怎麼填

- ticker：股票代號，例如 MU、NVDA、AAPL。
- quantity：成交股數。
- price：你的真實成交價。
- note：備註，可留空。
- profile：monster 或 swing；不填時預設 monster。
- limit：要顯示幾筆資料；/trades 與 /executions 預設 5，最大 20。
- tickers：可一次放多檔股票，空白或逗號分隔都可以。
- mode：bedtime / morning / opening / watchlist。
- debug：true / false；true 會顯示模型與搜尋管線檢查資訊。

範例：

```text
/buy MU 100 103.5 note=盤中試單 profile=monster
/buy MU 100 103.5 note=打算抱幾天 profile=swing
/add MU 50 104.2 profile=swing
/sell MU 100 108.8 profile=swing
/trades MU 10
/executions NVDA 8
/watchadd MU, NVDA, TSLA
/watchlist MU NVDA
/recap bedtime
/recap morning debug=true
/recap watchlist tickers=MU NVDA
/recapstatus
```

## 最簡單的使用流程

```text
開盤前先看 /positions
有成交就回報 /buy /add /sell
想看今晚或盤前結論就用 /recap bedtime 或 /recap morning
想看開盤驗證就用 /recap opening
想看 ai_decision + 持倉 + watchsaved 整合排序就用 /recap watchlist（/watchlist 也是同樣輸出）
想看上一張 recap 是否真的命中 AI/搜尋層就用 /recapstatus
看最近你自己怎麼買賣就用 /trades
看系統最近怎麼判斷就用 /executions
```

## Recap 使用方式（手動）

- `/recap bedtime`：睡前結論卡（含今晚決策與明早執行重點）
- `/recap morning`：隔夜結論卡（含過夜變化與盤前計畫）
- `/recap opening`：開盤驗證結論卡（先驗證再動作）
- `/recap watchlist`：watchsaved + ai_decision + 持倉整合卡

`/watchlist` 現在是 `/recap watchlist` 的同輸出別名，方便舊習慣繼續用。

補充：bedtime / morning / opening recap 都會把 watchsaved 與 ai_decision 一起納入追蹤查核，不是只看單一來源。

### 如何驗證有走 Gemini + Tavily

用 `/recap ... debug=true`，回傳尾端會有 `[Recap Debug]`：

- `gemini_enabled=true/false`
- `tavily_enabled=true/false`
- `ai_summary_generated=true/false`
- `tracked_news_count` / `conflict_news_count`

這代表「本次執行時」是否具備模型與搜尋層，以及這次卡片是否有新聞查核資料。

## Discord 通知時間（台灣時間）

### 盤中風控提醒（自動）

- 夏令時間：約 21:20 到隔天 05:10 之間，每 5 分鐘掃一次
- 冬令時間：約 22:20 到隔天 06:10 之間，每 5 分鐘掃一次

所以盤中提醒不是秒推，是批次掃描後推送。

### Swing 風控提醒

- 每個美股交易日收盤後掃一次
- 台灣時間大約是隔天 05:15

## 正式驗收收尾（5 日真實 artifact）

這一段是正式驗收用，不是概念測試。

每天固定跑一次正式 non-dry-run 全鏈路：

```powershell
python .\scripts\run_daily_production_acceptance.py --recap-mode bedtime
```

這個指令會依序執行：

- Monster（non-dry-run）
- Swing（non-dry-run）
- Recap（預設 bedtime）
- Notification reconciliation
- Canonical acceptance gate

每天都會保存 artifact 到：

- `repo_outputs/backtest/canonical/canonical_action_event_log.csv`
- `repo_outputs/backtest/canonical/notification_reconciliation_latest.json`
- `repo_outputs/backtest/canonical/acceptance_gate_latest.json`
- `repo_outputs/backtest/canonical/production_chain_summary_latest.json`
- `repo_outputs/backtest/canonical/daily/`（每日快照）

### 累積到第 5 個真實交易日後，怎麼跑 gate

先看最近是否已經累積到 5 日：

```powershell
python .\scripts\verify_canonical_chain_last5.py
```

如果顯示可用交易日已達 5，再執行正式 gate：

```powershell
python .\scripts\run_canonical_acceptance_gate.py
```

或用嚴格模式一次跑完整日鏈路（gate 不綠會直接 non-zero）：

```powershell
python .\scripts\run_daily_production_acceptance.py --recap-mode bedtime --strict-gate
```

只有當 `run_canonical_acceptance_gate.py` 回傳 `ok=true`，且 5 日真實 artifact 齊全時，才可視為正式驗收完成。

在 gate 轉綠前，不可標示為 completed，也不可當作正式 release 完成。

## 你只要記住這幾句

- 系統不會自動下單，只會提醒你。
- 真實成交後一定要回報 Discord。
- price 要填你的真實成交價，不要亂填。
- stop_loss 賣在買價下方是正常風控，不是 bug。
- take_profit 和 swing_reduce 通常代表已有浮盈或先保利。
- 盤中提醒不是秒推，因為現在是每 5 分鐘跑一次。
