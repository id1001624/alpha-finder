# 🎯 Alpha Finder 每日情報掃描腳本 v2.0

基於 Finviz + Yahoo Finance + Finnhub 的美股掃描系統，自動生成每日精選報告並上傳 Google Sheets，供 AI 分析使用。

---

## 📋 功能特色

✅ **完全免費** - 使用 Finviz + Yahoo Finance + Finnhub 免費 API  
✅ **三大掃描** - 起飛清單、財報預熱、預測情報  
✅ **自動評級** - AI/半導體/資料中心等優先產業 A 級評級  
✅ **全量數據** - 依設定上傳完整數據（預設 120 檔），供 AI 分析  
✅ **本地每日輸出** - 每次掃描自動輸出到 `repo_outputs/daily_refresh`（推薦給 AI 直接讀）  
✅ **Windows 排程** - 每日自動執行，結果就在 Google Sheets

---

## 🚀 快速開始

### 1️⃣ 安裝依賴套件

```bash
pip install -r requirements.txt
```

### 2️⃣ 設定 Finnhub API（選用）

PowerShell：

```powershell
[System.Environment]::SetEnvironmentVariable("FINNHUB_API_KEY", "YOUR_API_KEY", "User")
```

重新開啟 VS Code 或 PowerShell 後生效。

### 3️⃣ 執行掃描

```bash
python main.py
```

### 3.5️⃣ 啟動 TradingView Webhook Server（可選，但 Track F 需要）

先設定環境變數（PowerShell）：

```powershell
[System.Environment]::SetEnvironmentVariable("TV_WEBHOOK_SECRET", "YOUR_SECRET", "User")
[System.Environment]::SetEnvironmentVariable("SIGNAL_STORE_PATH", "signals.db", "User")
```

啟動 server：

```bash
python server.py
```

或：

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

### 4️⃣ 查看結果

執行完成後，到 Google Sheets 查看：

- **`全量數據`** tab - 依設定輸出完整數據，供 AI 分析
- **`YYYY-MM-DD`** tab - 當日三合一精選報告（起飛 Top 3 + 財報 Top 3 + 預測 Top 3）

同時會在專案本地輸出（建議給 AI 直接讀 6 檔）：

- `repo_outputs/ai_ready/latest/ai_focus_list.csv`
- `repo_outputs/ai_ready/latest/fusion_top_daily.csv`
- `repo_outputs/ai_ready/latest/raw_market_daily.csv`
- `repo_outputs/ai_ready/latest/theme_heat_daily.csv`
- `repo_outputs/ai_ready/latest/theme_leaders_daily.csv`
- `repo_outputs/ai_ready/latest/xq_short_term_updated.csv`

> 預設已停用「全量數據」上傳到 Google Sheets，避免 AI 雲端讀取誤差。

### 5️⃣ 一次執行（建議每日流程）

```bash
run_daily.bat
```

這個批次流程會自動完成：

1. `python main.py`
2. `python scripts/update_xq_with_history.py`

你一天只要做 3 件事（固定不變）：

1. （可選）先確認 TradingView webhook server 正在跑（要用 TV 指標時才需要）
2. 執行 `run_daily.bat`
3. 把 `repo_outputs/ai_ready/latest` 的 6 檔 CSV、Alpha-Sniper-Protocol-v8.md 一次餵給 AI，直接要最終結論

補充（避免放錯檔案位置）：

- `ai_focus_list.csv` 不用手動放，`run_daily.bat` 會自動更新在 `repo_outputs/ai_ready/latest/ai_focus_list.csv`
- 網頁 AI 回傳的 `FILE: ai_decision_YYYY-MM-DD.csv`，請存到 `repo_outputs/backtest/inbox/`
- 存好後執行：

```bash
python scripts/record_ai_decision.py --auto-latest
```

這樣會自動更新：

- `repo_outputs/backtest/ai_decision_log.csv`（AI 決策歷史）
- `repo_outputs/backtest/daily_ai_decisions/YYYY-MM-DD_ai_decision.csv`（每日快照）
- `repo_outputs/backtest/ai_decision_latest.csv`（最新一份）

每週額外自動化（零人工）：

```bash
run_weekly_review.bat
```

會輸出：

- `repo_outputs/backtest/weekly_reports/weekly_report_latest.md`（單一週報：Local / Local-Fusion / AI 三軌）
- `repo_outputs/backtest/weekly_reports/weekly_trades_latest.csv`（Local 自動交易日誌）
- `repo_outputs/backtest/weekly_reports/weekly_fusion_trades_latest.csv`（Local-Fusion 自動交易日誌）
- `repo_outputs/backtest/weekly_reports/weekly_ai_trades_latest.csv`（AI 自動交易日誌）

三個策略定義：

- `Local`：只使用 `xq_pick_log.csv`
- `Local-Fusion`：同日合併 `xq_pick_log + ai_focus_list`（同 ticker 去重後回測）
- `AI`：使用 `ai_decision_log.csv`

週報中的差值欄位說明：

- `ai_minus_fusion_*`：AI - Local-Fusion（正值代表 AI 較佳）
- `ai_fusion_drawdown_improve`：Local-Fusion 最大回撤 - AI 最大回撤（正值代表 AI 回撤較小）

### 5.1️⃣ 為什麼會生成很多 CSV？

這些 CSV 用於不同目的，不是都給 AI 讀：

- `daily_refresh/*`：完整日更產線（含除錯/審核/回放）
- `backtest/*`：歷史回測與績效統計
- `ai_ready/latest/*`：固定給 AI 的 6 檔最小集合（你每天只看這裡）

回測專用（不餵 AI）：

- `repo_outputs/backtest/xq_pick_log.csv`（歷史累積主檔）
- `repo_outputs/backtest/ai_decision_log.csv`（AI 決策歷史主檔）
- `repo_outputs/backtest/daily_xq_picks/YYYY-MM-DD_xq_top_picks.csv`（每日快照）
- `repo_outputs/backtest/weekly_reports/weekly_report_latest.md`（每週制度化評估，含三軌比較）
- `repo_outputs/backtest/weekly_reports/weekly_trades_latest.csv`（每週 Local 交易日誌）
- `repo_outputs/backtest/weekly_reports/weekly_fusion_trades_latest.csv`（每週 Local-Fusion 交易日誌）
- `repo_outputs/backtest/weekly_reports/weekly_ai_trades_latest.csv`（每週 AI 交易日誌）

盤前快檢（終端）：

```bash
python scripts/premarket_volume_check.py --symbols NVAX,FA,CBZ
```

直接吃 XQ 記錄名單回測（推薦）：

```bash
python tests/backtest_winrate.py --mode xq-pick-log --start 2026-01-01 --end 2026-03-01 --hold-days 1
```

比較 rank 區間勝率（rank 1-3 vs rank 4-10）：

```bash
python tests/backtest_winrate.py --mode xq-pick-log --start 2026-01-01 --end 2026-03-01 --hold-days 1 --by-rank-report
```

`run_daily.bat` 目前只負責產生 AI 所需 6 檔 CSV；AI 決策歸檔需另外執行 `record_ai_decision.py`。

---

## 📡 TradingView Webhook 設定

### Endpoint 與驗證

- Endpoint：`POST /tv/webhook`
- 本機 URL：`http://127.0.0.1:8000/tv/webhook`
- 公網 URL（部署後）：`https://<your-domain>/tv/webhook`
- 驗證方式：
	- `X-Webhook-Token: <TV_WEBHOOK_SECRET>`（簡單）或
	- `X-TV-Signature: <hmac_sha256(raw_body)>`

### TradingView Alert Message（請填 JSON）

> TradingView Webhook body 直接等於 Alert Message；請務必填 JSON 字串。

```json
{
	"schema_version": 1,
	"source": "tradingview",
	"symbol": "AAPL",
	"exchange": "NASDAQ",
	"timeframe": "1D",
	"ts": "2026-02-26T14:30:00Z",
	"close": 188.2,
	"vwap": 187.9,
	"sqz_on": true,
	"sqzmom_value": 0.31,
	"sqzmom_color": "green",
	"event": "entry"
}
```

### 訊號資料落地位置

- SQLite 檔案：`SIGNAL_STORE_PATH`（預設 `signals.db`）
- 主要資料表：`signals`
- 若允許純文字 webhook（`ALLOW_PLAIN_TEXT_WEBHOOK=true`）且非 JSON，會記錄到 `raw_webhook_logs`

### 如何檢查最新訊號是否進來

```bash
python -c "from signal_store import get_latest_signals; from datetime import datetime, timezone; print(get_latest_signals('signals.db', asof=datetime.now(timezone.utc), max_age_minutes=240))"
```

### TradingView 指標快速自檢（建議先跑一次）

1. 開 server（另一個終端）：

```bash
python server.py
```

2. 在 PowerShell 送一筆測試 webhook：

```powershell
$secret=$env:TV_WEBHOOK_SECRET; $payload=@{schema_version=1;source='tradingview';symbol='AVAV';exchange='NASDAQ';timeframe='1D';ts=(Get-Date).ToUniversalTime().ToString('o');close=150.12;vwap=149.5;sqz_on=$true;sqzmom_value=0.42;sqzmom_color='green';event='entry'} | ConvertTo-Json -Compress; Invoke-RestMethod -Uri 'http://127.0.0.1:8000/tv/webhook' -Method Post -ContentType 'application/json' -Headers @{'X-Webhook-Token'=$secret} -Body $payload
```

3. 確認資料入庫：

```bash
python -c "import sqlite3; conn=sqlite3.connect('signals.db'); cur=conn.cursor(); cur.execute('select count(*) from signals'); print(cur.fetchone()); cur.execute('select symbol, ts, received_at from signals order by received_at desc limit 1'); print(cur.fetchone()); conn.close()"
```

若 `signals` 仍是 0，優先檢查：

- TradingView Alert URL 是否正確指向 `/tv/webhook`
- `TV_WEBHOOK_SECRET` 是否與 TradingView Header 一致
- server 是否真的在收外網請求（本機測試可過，但外網打不到）

---

## 🌐 公開 HTTPS 部署（Cloudflare Tunnel 範例）

1. 本機先啟動：`uvicorn server:app --host 0.0.0.0 --port 8000`
2. 安裝並登入 `cloudflared`
3. 建立 tunnel 指向本機 8000：`cloudflared tunnel --url http://localhost:8000`
4. 取得 `https://xxxx.trycloudflare.com/tv/webhook`，填到 TradingView Webhook URL

> TradingView 僅接受 80/443 且要求快速回應；本專案 webhook 會先驗證+落地後立即回 200。

---

## 📊 掃描邏輯

### Sheet 1 - 起飛清單（Top 10）

**篩選條件**：
- ✅ 今日漲幅 > 3%
- ✅ 成交量 > 過去 5 日平均量的 1.8 倍
- ✅ 股價 > $5
- ✅ 市值 > 1 億美元
- ✅ 排除槓桿 ETF、REIT

**輸出欄位**：
| 股票代碼 | 漲幅% | 量能倍數 | 市值 | 現價 | 產業 | 新聞標題 | 評級 |
|---------|------|---------|-----|-----|-----|---------|-----|

**評級邏輯**：
- **A 級**：優先產業 + 量能 > 2.5x
- **B 級**：優先產業
- **C 級**：符合基本條件

---

### Sheet 2 - 財報預熱（Top 10）

**篩選條件**：
- ✅ 未來 7 天內發財報
- ✅ 市值 > 10 億美元
- ✅ 優先產業：AI/半導體/資料中心/工業/電力設備/醫療服務/生技

**輸出欄位**：
| 股票代碼 | 財報日期 | 預估EPS | 市值 | 產業 | 目標價 | 評級 |
|---------|---------|--------|-----|-----|-------|-----|

---

### Sheet 3 - 預測情報（Top 10）

**篩選條件**：
- ✅ 分析師目標價 > 現價 30%
- ✅ 至少 3 位分析師覆蓋

**輸出欄位**：
| 股票代碼 | 事件類型 | 目標價 | 預期漲幅% | 分析師數 | 產業 | 評級 |
|---------|---------|-------|----------|---------|-----|-----|

**評級邏輯**：
- **A 級**：上漲空間 > 50%
- **B 級**：上漲空間 30%-50%

---

## ⚙️ 進階配置

編輯 `config.py` 可自訂以下參數：

```python
# 爬取頁數（每頁 20 筆）
MAX_PAGES = 5

# 最多處理幾檔股票
MAX_STOCKS_TO_PROCESS = 120

# 篩選條件
LAUNCH_MIN_GAIN = 3.0          # 起飛清單最低漲幅 %
LAUNCH_MIN_REL_VOL = 1.8       # 最低量能倍數
EARNINGS_DAYS_AHEAD = 7        # 財報預熱天數
ANALYST_MIN_UPSIDE = 30.0      # 預測情報最低上漲空間 %
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")  # Finnhub API Key

# TradingView 訊號整合
USE_TRADINGVIEW_SIGNALS = True
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
SIGNAL_STORE_PATH = os.getenv("SIGNAL_STORE_PATH", "signals.db")
SIGNAL_MAX_AGE_MINUTES = 240
```

---

## 📦 依賴套件

| 套件 | 用途 |
|-----|------|
| `pandas` | 資料處理 |
| `yfinance` | Yahoo Finance API |
| `finviz>=2.0.0` | Finviz 篩選 + 分析師目標價 |
| `gspread` | Google Sheets 集成 |
| `oauth2client` | Google 服務帳號認證 |
| `lxml` | XML 解析 |
| `requests` | Finnhub API 呼叫 |

---

## 🔧 常見問題

### Q0: 盤前要怎麼快速比對量能？

```bash
python scripts/premarket_volume_check.py --symbols NVAX,FA,CBZ
```

若你在 TradingView 有更即時盤前量，可手動覆蓋：

```bash
python scripts/premarket_volume_check.py --symbols NVAX,FA --manual-premarket NVAX=120000,FA=80000
```

### Q1: Finviz 爬取失敗？

**解決方案**：
- 檢查網路連線
- 減少 `MAX_PAGES` 數量（改為 1-2 頁）
- 增加延遲時間（在 `scrape_finviz_screener()` 中調整 `time.sleep(3)`）

### Q2: Yahoo Finance API 限制？

**解決方案**：
- 減少 `MAX_STOCKS_TO_PROCESS`（改為 20-30）
- 增加 `API_DELAY`（在 `config.py` 中改為 1.0 秒）

### Q3: 沒有符合條件的股票？

**原因**：
- 市場當日沒有符合條件的股票
- 篩選條件太嚴格

**解決方案**：
- 降低 `LAUNCH_MIN_GAIN`（改為 2.0%）
- 降低 `LAUNCH_MIN_REL_VOL`（改為 1.5）

### Q4: SSL 憑證錯誤（curl: 77）？

安裝路徑含中文時 curl 無法讀取憑證，`main.py` 與 `scripts/update_xq_with_history.py` 都已內建 `_fix_ssl_cert_path()` 自動修復，不需要手動處理。

---

## 🚀 進階功能

### 1️⃣ Google Sheets 設定

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立服務帳號，下載 `credentials.json` 放入專案目錄
3. 建立名為 `Alpha_Sniper_Daily_Report` 的試算表
4. 分享給服務帳號 email（credentials.json 裡的 `client_email`）
5. `config.py` 中設定 `GSHEET_ENABLED = True`

### 2️⃣ Windows Task Scheduler 每日自動執行

一鍵建立（推薦）：

```bash
setup_schtasks.bat
```

執行一次後就會註冊 Daily + Weekly 兩個工作；若你不想保留這個安裝器，可刪除 `setup_schtasks.bat`，不影響既有排程。

在 PowerShell 執行（設定每天 13:00，確保電腦開啟時段）：

```powershell
schtasks /create /tn "AlphaFinder_Daily" /tr "\"C:\Users\w6359\OneDrive\文件\alpha-finder\run_daily.bat\"" /sc daily /st 13:00 /it /f
```

每週制度化評估（建議週日 16:00）：

```powershell
schtasks /create /tn "AlphaFinder_WeeklyReview" /tr "\"C:\Users\w6359\OneDrive\文件\alpha-finder\run_weekly_review.bat\"" /sc weekly /d SUN /st 16:00 /it /f
```

執行結果記錄在 `run_log.txt`（同目錄）。

---

## 📈 實際案例

### 範例輸出（2026-02-23）

**起飛清單 Top 3**：
1. **NVDA** - 漲幅 5.2% | 量能 3.1x | 評級 A
2. **AMD** - 漲幅 4.8% | 量能 2.7x | 評級 A
3. **SMCI** - 漲幅 6.1% | 量能 4.2x | 評級 A

**財報預熱 Top 3**：
1. **TSLA** - 財報 2026-02-25 | 產業 汽車 | 評級 B
2. **AAPL** - 財報 2026-02-27 | 產業 科技 | 評級 A
3. **GOOGL** - 財報 2026-02-28 | 產業 通訊 | 評級 A

**預測情報 Top 3**：
1. **PLTR** - 上漲空間 52.3% | 目標價 $45.00 | 評級 A
2. **IONQ** - 上漲空間 48.7% | 目標價 $38.50 | 評級 B
3. **MSTR** - 上漲空間 41.2% | 目標價 $520.00 | 評級 B

---

## ⚠️ 免責聲明

本工具僅供研究學習使用，不構成投資建議。
股票投資有風險，請謹慎評估後再做決策。
作者不對使用本工具產生的任何損失負責。

---

## 📝 更新日誌

### v2.0 (2026-02-24)
- ✅ 完全重寫，修復中文路徑 SSL 問題
- ✅ 信號強度排序（漲幅×2 + 量能×3），不再字母順序
- ✅ 上傳全量 40 檔數據到 Google Sheets（供 AI 分析）
- ✅ 移除 CSV 輸出（改為純 GSheets）
- ✅ 移除視覺化模組
- ✅ Windows Task Scheduler 自動排程

### v1.0 (2026-02-23)
- ✅ 首次發布

---

## 📧 聯絡方式

遇到問題或有建議？歡迎回報！

**作者**：Alpha Sniper Team  
**版本**：v2.0  
**最後更新**：2026-02-24

---

## 🙏 致謝

感謝以下開源專案：
- [Finviz](https://finviz.com/) - 免費股票篩選器
- [yfinance](https://github.com/ranaroussi/yfinance) - Yahoo Finance API
- [Pandas](https://pandas.pydata.org/) - 資料分析工具

---

**🚀 Happy Trading! 祝你交易順利！**