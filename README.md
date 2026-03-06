# Alpha Finder 個人操作手冊

## 這個專案在做什麼

這個專案是我的 AI Trading 情報引擎（不自動下單），負責：

- 每天找可能爆發的股票
- 做多層排序（feature/radar/event/ranking/decision）
- 輸出給我和 AI 做研究判斷

核心分工：

- 找股票：Scanner / Research / Ranking
- 交易時機：VWAP / SQZMOM（輔助，不是核心找股）

## 我每天要做什麼

1. 跑日更（主產線）

```powershell
.\run_daily.bat
```

1. 跑雙策略比較（建議每天都跑）

```powershell
.\run_ai_compare.bat web
```

1. 看比較結果（決定今天偏向哪個 profile）

- `repo_outputs/ai_trading/profile_compare/latest/profile_compare_summary.md`

1. 把研究檔餵給網頁 AI（見下方「丟哪些檔案」）

## Web / API 開關

`AI_RESEARCH_MODE` 只填模式字串：

- `web`：免費、預設
- `api`：網頁額度用完時切換（Tavily + Gemini）

兩種模式現在都走同一份決策契約：

- 最終都應產出 `ai_decision_YYYY-MM-DD.csv`
- 同一份 CSV 固定包含催化欄位（`research_mode`、`catalyst_type`、`catalyst_sentiment`、`explosion_probability`、`hype_score`、`confidence`、`api_final_score`、`catalyst_source`、`catalyst_summary`）
- `record_ai_decision.py` 歸檔時會保留以上欄位，並在可用時自動從 `api_catalyst_analysis_daily.csv` 補齊缺漏值
- `api` 模式現在會直接產出最終 `ai_decision_YYYY-MM-DD.csv` 到 `repo_outputs/backtest/inbox/`

即時切換（目前終端）：

```powershell
$env:AI_RESEARCH_MODE='web'   # 或 api
```

補充：

- 只要 `AI_RESEARCH_MODE='api'`，就會啟用 API 備援決策流程
- 若你真的想強制關閉 API 偵測器，才另外設：`$env:CATALYST_DETECTOR_ENABLED='false'`

## 要丟給網頁 AI 哪些檔案

- `repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx`
- `Alpha-Sniper-Protocol-v8.md`

說明：

- `ai_ready_bundle.xlsx` 現在是統一入口，已合併原本 A 的關鍵訊號（feature/radar/event/ranking/decision）與原本 B 的核心表。
- 日常只要上傳上面 2 個檔案即可，網頁 AI 回答最後必須輸出 `ai_decision_YYYY-MM-DD.csv`。

## ai_decision_YYYY-MM-DD.csv 要放哪裡

網頁 AI 產生後，放到：

- `repo_outputs/backtest/inbox/`

以下指令都假設你現在就在專案根目錄，且終端已啟用 `(.venv)`。

然後執行：

```powershell
python .\scripts\record_ai_decision.py --auto-latest
```

補充：

- 即使你用 `web` 模式，`ai_decision` 也要維持同一份欄位結構
- 若你改用 `api` 模式，歸檔時會自動把 `repo_outputs/ai_trading/latest/api_catalyst_analysis_daily.csv` 可對應的催化欄位補進 `ai_decision_log.csv`

`--auto-latest` 會依序搜尋：

- `repo_outputs/backtest/inbox/`
- `repo_outputs/ai_ready/latest/`
- `repo_outputs/daily_refresh/latest/`

## 第二、三層通知（Discord/LINE）

當你已產生 `ai_decision_YYYY-MM-DD.csv` 後，可以直接發即時通知：

先測試（只預覽，不送出）：

```powershell
python .\scripts\push_alerts_from_ai_decision.py --auto-latest --dry-run
```

送 Discord（推薦）：

```powershell
$env:DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/你的URL'
python .\scripts\push_alerts_from_ai_decision.py --auto-latest --channel discord --top-n 5 --tags keep,watch
```

送 LINE（Messaging API）：

```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN='你的token'
$env:LINE_TO_USER_ID='你的userId'
python .\scripts\push_alerts_from_ai_decision.py --auto-latest --channel line --top-n 5 --tags keep,watch
```

補充：

- 腳本會自動讀取 TradingView 已落地到 `signals.db` 的最新 VWAP/SQZMOM 訊號，合併進通知文字。
- `--channel both` 可同時推 Discord + LINE。

## 第四層追蹤（回寫位置）

通知和決策不要貼回聊天室，直接寫檔追蹤：

- 決策主檔：`repo_outputs/backtest/ai_decision_log.csv`
- 通知紀錄：`repo_outputs/backtest/alerts/alert_log.csv`
- 最新通知文字：`repo_outputs/backtest/alerts/latest_alert_message.txt`

每週覆盤時，把 `ai_decision_log.csv` + `weekly_report_latest.md` 一起看，就能回頭檢查「有通知但沒進場」「有進場但停損/停利執行」的差異。

## 每週回測資料多久丟給你一次

建議每 7 天一次（固定週日或每 5 個交易日）。

每週丟這些就夠：

- `repo_outputs/backtest/weekly_reports/weekly_report_latest.md`
- `repo_outputs/backtest/weekly_reports/weekly_trades_latest.csv`
- `repo_outputs/backtest/weekly_reports/weekly_ai_trades_latest.csv`
- `repo_outputs/ai_trading/profile_compare/latest/profile_compare_summary.csv`

若連續 2-3 天異常，提早丟，不用等一週。

## Auto-hybrid v2 提醒（下週）

下週要做：Auto-hybrid v2

目標：

- 用最近 2-4 週比較結果自動調權重
- 減少手動判斷當日走 balanced 或 monster_v1
- 產出單一主策略 + 備援清單

## 快速自檢

```powershell
python -c "import config; print(config.AI_RESEARCH_MODE, config.CATALYST_DETECTOR_ENABLED, bool(config.TAVILY_API_KEY), bool(config.GEMINI_API_KEY), config.GEMINI_MODEL)"
```
