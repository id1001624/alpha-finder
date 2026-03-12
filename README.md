# Alpha Finder — 無腦投資操作版

你平常只做 2 件事：

1. 每天產出最新決策
2. 有真實成交時立刻回報 Discord

其他像盤中監控、風控提醒、早晨摘要、開盤摘要、Swing 掃描，都是系統自動跑。

## 每天怎麼操作

### 1. 跑主產線

```powershell
.\run_daily.bat
```

### 2. 把這 2 個檔案丟給網頁 AI

- repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx
- Alpha-Sniper-Protocol.md

### 3. 把 AI 回傳的決策檔放回 repo

把 ai_decision_YYYY-MM-DD.csv 放進：

- repo_outputs/backtest/inbox/

### 4. 歸檔決策

```powershell
python .\scripts\record_ai_decision.py --auto-latest
```

如果同一天你重做了一份新版決策，想整份覆蓋舊版：

```powershell
python .\scripts\record_ai_decision.py --auto-latest --replace-date
```

做完這 4 步，接下來只要看 Discord。

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
  把最新 ai_decision、你的持倉、保存關注股、臨時輸入股票整合成乾淨排序。
  tickers 可不填；如果要填，就是臨時多加幾檔一起比較。

- /watchadd tickers
  把股票加入你自己的保存關注清單。
  可以一次加多檔，空白或逗號分隔都可以，例如：AAPL NVDA TSLA。

- /watchremove tickers
  從保存關注清單移除股票。
  可以一次刪多檔，空白或逗號分隔都可以。

- /watchsaved
  看你目前保存的關注股。

## 參數到底怎麼填

- ticker：股票代號，例如 MU、NVDA、AAPL。
- quantity：成交股數。
- price：你的真實成交價。
- note：備註，可留空。
- profile：monster 或 swing；不填時預設 monster。
- limit：要顯示幾筆資料；/trades 與 /executions 預設 5，最大 20。
- tickers：可一次放多檔股票，空白或逗號分隔都可以。

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
```

## 最簡單的使用流程

```text
開盤前先看 /positions
有成交就回報 /buy /add /sell
想快速整理今天重點就看 /watchlist
看最近你自己怎麼買賣就用 /trades
看系統最近怎麼判斷就用 /executions
```

## Discord 通知時間（台灣時間）

### 固定摘要

- Bedtime recap：每天 22:17
- Morning recap：每天 07:17
- Watchlist follow-up：每天 07:22

### Opening recap

- 夏令時間：約 21:38、21:46
- 冬令時間：約 22:38、22:46

這個摘要本來就不是一開盤立刻發，而是故意等開盤後幾分鐘再驗證一次。

### 盤中風控提醒

- 夏令時間：約 21:20 到隔天 05:10 之間，每 5 分鐘掃一次
- 冬令時間：約 22:20 到隔天 06:10 之間，每 5 分鐘掃一次

所以盤中提醒不是秒推，是批次掃描後推送。

### Swing 風控提醒

- 每個美股交易日收盤後掃一次
- 台灣時間大約是隔天 05:15

## 你只要記住這幾句

- 系統不會自動下單，只會提醒你。
- 真實成交後一定要回報 Discord。
- price 要填你的真實成交價，不要亂填。
- stop_loss 賣在買價下方是正常風控，不是 bug。
- take_profit 和 swing_reduce 通常代表已有浮盈或先保利。
- 盤中提醒不是秒推，因為現在是每 5 分鐘跑一次。
