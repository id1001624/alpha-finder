# Alpha Finder - Copilot Instructions

## Project Overview

**Alpha Finder** 是一個免費的 S&P 500 股票每日掃描系統，使用 Finviz + Yahoo Finance 標準 API 自動找出「起飛信號、財報預熱、預測情報」三大交易訊號。所有結果自動上傳 Google Sheets，供 AI 分析使用。

**架構核心**：
```
Finviz（503 檔股票） 
  ↓ 按信號強度排序（漲幅×2 + 量能×3）
Yahoo Finance（前 40 強詳細數據）
  ↓ 三個並行篩選軌道
Google Sheets（全量 40 檔 + 精選 Top 3）
```

**關鍵設計選擇**：
- 不輸出 CSV，改為純 GSheets（AI 才是讀者，減少磁碟操作）
- 信號強度排序優先於字母順序，避免完全錯過啟動初期股票
- 軌道保持不同門檻（3%/1.8x vs XQ 的 4.5%/2.5x），便於雙重確認

---

## Code Architecture

### 1. Config-Driven Design

**檔案**: `config.py`（38 行）
- 所有業務邏輯數字都是 config 常數，不寫死在 main.py
- 例：`LAUNCH_MIN_GAIN = 3.0`, `TOP_N_STOCKS = 3`
- **關鍵慣例**：修改任何篩選邏輯都從 config 開始，不改 main.py

### 2. Three-Track Filtering Pipeline

| Track | 函數 | 條件 | 用途 |
|-------|------|------|------|
| **起飛清單** | `filter_sheet1_launch()` | 漲幅 >3%, 量能 >1.8x | 當日爆漲 |
| **財報預熱** | `filter_sheet2_earnings()` | 7 天內財報, MCap >1B | 事件驅動 |
| **預測情報** | `filter_sheet3_analyst()` | 上漲空間 >30%, 分析師 ≥3 | 機構看好 |

**管道流程**（main() 第 973 行）：
```
scrape_finviz_screener()  # 爬 503 檔，排序 → DataFrame
  ↓
enrich_with_yfinance()    # 前 40 強查詳細資料（財報日、目標價、新聞）
  ↓
filter_sheet1/2/3()      # 三軌並行篩選，各輸出 Top 3
  ↓
GoogleSheetsUploader.upload_full_data() + upload_daily_report()
```

### 3. Signal Strength Scoring

**位置**：`scrape_finviz_screener()` 第 550 行
```python
df['_change_score'] = df['Daily_Change'].clip(lower=0)          # 負漲幅視為 0
df['_vol_score'] = (df['Rel_Volume'] - 1).clip(lower=0)         # 只計超過 1 的部分
df['_signal_score'] = df['_change_score'] * 2 + df['_vol_score'] * 3
df = df.sort_values('_signal_score', ascending=False)
```
**為何**：防止「負漲幅但量能大」的股票排在「微漲但起動中」的前面，漲幅權重加倍。

### 4. Priority Sector Classification

**位置**：`_apply_sector_classification()` 第 690 行
```python
PRIORITY_KEYWORDS = {
    'AI/半導體': ['ai', 'chip', 'gpu', ...],
    '資料中心': ['data center', ...],
    ...
}
```
**3-tier rating**：
- **A 級**：優先產業 + 量能 >2.5x（只給起飛清單最強訊號）
- **B 級**：優先產業或分析師看好
- **C 級**：基本條件達到

### 5. SSL Certificate Workaround

**位置**：`_fix_ssl_cert_path()` 第 15-52 行
- **問題**：Windows 中文路徑（"文件"含 UTF-8）導致 curl 無法讀 CA 憑證
- **解決**：啟動時將 certifi 憑證複製到 `~/.alpha_finder_certs/cacert.pem`（純 ASCII），設定 `CURL_CA_BUNDLE=` env var
- **重要**：部署到其他環境時，如果路徑含非 ASCII 字符，此函數會自動激活

---

## Development Workflow

### Run Daily Scan
```bash
python main.py
```
- 標準輸出顯示摘要（5 檔強勢股、三軌結果）
- 無 CSV 輸出，全部上傳到 GSheets (`Alpha_Sniper_Daily_Report`)
- 建立兩個 tab：`全量數據`（40 檔完整）+ `YYYY-MM-DD`（精選 Top 3）

### Automated Execution (Windows Task Scheduler)
```powershell
schtasks /create /tn "AlphaFinder_Daily" /tr "\"C:\...\run_daily.bat\"" /sc daily /st 05:30 /ru SYSTEM /f
```
- 每天 05:30（美股收盤後 1.5h）自動跑
- log 記在 `run_log.txt`

### Debug Tips
1. **SSL 失敗**：檢查 `~/.alpha_finder_certs/cacert.pem` 是否存在，若路徑含中文重新跑 `_fix_ssl_cert_path()`
2. **API 限制**：減少 `MAX_STOCKS_TO_PROCESS`（改為 20），增加 `API_DELAY`（改為 1.0）
3. **GSheets 認證失敗**：檢查 `credentials.json` 是否放在專案根目錄，確認服務帳號被分享到 GSheets

---

## Project-Specific Patterns

### Pattern 1: Config-First Refactoring
新增篩選條件時，**先加到 config.py**，再在對應 filter_sheet*() 函數裡使用。永不硬碼。

### Pattern 2: Dataframe Chaining
每個 filter/enrich 函數用 pandas iterator `iterrows()` 逐筆處理，返回乾淨的 DataFrame。
```python
for idx, row in df.iterrows():
    # 邏輯
    enriched.append({**row.to_dict(), 'new_col': value})
return pd.DataFrame(enriched)
```

### Pattern 3: Error Graceful Degradation
缺少欄位（如新聞標題）時用空字串或 None，不拋例外：
```python
enriched_data.append({
    **row.to_dict(),
    'News_Headline': '',  # 找不到新聞就空
    'Earnings_Date': None,
})
```

### Pattern 4: GSheets Column Mapping
上傳前重新對應欄名（CONFIG 保持英文，GSheets 輸出用中文）：
```python
cols_map = {
    'Ticker': '股票代碼',
    'Daily_Change': '今日漲幅%',
    ...
}
available = {k: v for k, v in cols_map.items() if k in df.columns}
output = df[list(available.keys())].copy()
output.columns = list(available.values())
```

---

## External Dependencies & Integration Points

### API Limits & Retry Logic
- **Finviz**：每天 ~1 次爬取，爬 3 頁（60 檔採樣），無明顯速率限制
- **Yahoo Finance**：最多查 40 檔，每筆 0.5s 延遲（可在 config 調整）
- **重試機制**：`retry_on_failure()` 用指數退避，3 次失敗才放棄

### Google Sheets Service Account
- 必需 `credentials.json`（服務帳號 key）在專案根目錄
- 試算表名稱必須叫 `Alpha_Sniper_Daily_Report`
- 該帳號必須被分享 edit 權限到目標試算表

### Finviz Library (mariostoev v2.0.0)
- **Performance table**：當日漲幅、量能等
- **Overview table**：產業、市值、股價等
- 兩表合併得完整 DataFrame（位置：`scrape_finviz_screener()` 第 480 行）

---

## Common Modifications

### Add New Filter Track
1. `config.py` 新增篩選參數（如 `NEW_FILTER_MIN_X = 10.0`）
2. 複製 `filter_sheet1/2/3()` 其一，改邏輯+欄位對應
3. `main()` 第 990 行新增呼叫
4. `GoogleSheetsUploader.upload_daily_report()` 新增 sheet4 參數

### Adjust Signal Strength Formula
編輯 `scrape_finviz_screener()` 第 550 行排序邏輯（目前 change×2 + vol×3），改變權重係數。

### Change GSheets Output Format
編輯 `upload_full_data()` 和 `upload_daily_report()` 的欄位對應（`cols_map` dict）。

---

## Key Files Quick Reference

| 檔案 | 行數 | 職責 |
|------|------|------|
| `main.py` | 1002 | 核心邏輯，三軌篩選 + GSheets 上傳 |
| `config.py` | 80 | 所有可調參數，無業務邏輯 |
| `run_daily.bat` | 14 | Windows 排程執行入口 |
| `requirements.txt` | 14 | 依賴：pandas, finviz>=2.0, yfinance, gspread |
| `.github/copilot-instructions.md` | 本檔 | AI agent 指導 |

---

## Testing & Validation

**無正式單元測試**。驗證方式：
1. 執行 `python main.py`，檢查 terminal 摘要（5 檔 Top + 三軌結果）
2. 檢查 GSheets `全量數據` tab 有無 40 檔完整資料
3. 檢查 `YYYY-MM-DD` tab 有無精選 Top 3（順序應符合評級 A→B→C）

---

## Known Limitations & Future Considerations

- **單一指數**：目前綁定 S&P 500 (`idx_sp500`)，要擴充到 Russell 2000 需改 `SELECTED_INDICES`
- **XQ 整合**：刻意不整合 XQ 腳本，兩套工具獨立（便於雙重確認）
- **IPO 偵測**：無免費資料源，未實裝 IPO 預熱軌道
- **績效統計**：無回測、勝率統計，工具定位是「掃描+推薦」而非風控

