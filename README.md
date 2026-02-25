# 🎯 Alpha Finder 每日情報掃描腳本 v2.0

基於 Finviz + Yahoo Finance + Finnhub 的美股掃描系統，自動生成每日精選報告並上傳 Google Sheets，供 AI 分析使用。

---

## 📋 功能特色

✅ **完全免費** - 使用 Finviz + Yahoo Finance + Finnhub 免費 API  
✅ **三大掃描** - 起飛清單、財報預熱、預測情報  
✅ **自動評級** - AI/半導體/資料中心等優先產業 A 級評級  
✅ **全量數據** - 依設定上傳完整數據（預設 120 檔），供 AI 分析  
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

### 4️⃣ 查看結果

執行完成後，到 Google Sheets 查看：

- **`全量數據`** tab - 依設定輸出完整數據，供 AI 分析
- **`YYYY-MM-DD`** tab - 當日三合一精選報告（起飛 Top 3 + 財報 Top 3 + 預測 Top 3）

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

安裝路徑含中文時 curl 無法讀取憑證，main.py 的 `_fix_ssl_cert_path()` 會自動修復，不需要手動處理。

---

## 🚀 進階功能

### 1️⃣ Google Sheets 設定

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立服務帳號，下載 `credentials.json` 放入專案目錄
3. 建立名為 `Alpha_Sniper_Daily_Report` 的試算表
4. 分享給服務帳號 email（credentials.json 裡的 `client_email`）
5. `config.py` 中設定 `GSHEET_ENABLED = True`

### 2️⃣ Windows Task Scheduler 每日自動執行

在 PowerShell 執行（設定每天早上 05:30，美股收盤約 1.5 小時後）：

```powershell
schtasks /create /tn "AlphaFinder_Daily" /tr "\"C:\Users\w6359\OneDrive\文件\alpha-finder\run_daily.bat\"" /sc daily /st 05:30 /ru SYSTEM /f
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