# Finnhub API 設定指南

## 為什麼需要 Finnhub？

Alpha Finder v2.1 整合了 Finnhub 免費 API 來補強：

1. **財報日期準確性** - yfinance 可能無法取得未來財報，Finnhub 提供更準確的預估財報日期
2. **分析師目標價** - 補充 yfinance 缺失的目標價資料
3. **白名單股票保證** - 即使 NVDA 等龍頭股未出現在 Finviz 篩選結果，也會強制補抓

---

## 如何取得 Finnhub API Key（免費）

### 1️⃣ 註冊帳號

訪問：https://finnhub.io/register

- 使用 Email 註冊（或 Google 帳號快速登入）
- 免費方案：**60 API calls/分鐘**（每日足夠使用）

### 2️⃣ 複製 API Key

登入後，在 Dashboard 頁面找到：

```
API Key: xxxxxxxxxxxxxxxxxxxxxx
```

### 3️⃣ 配置到專案

編輯 `config.py`，找到這一行：

```python
FINNHUB_API_KEY = ""  # 請填入你的 Finnhub API Key
```

改為：

```python
FINNHUB_API_KEY = "你的API Key"
```

例如：
```python
FINNHUB_API_KEY = "co1234567890abcdef"
```

---

## 驗證配置

執行一次掃描，觀察 log 輸出：

```bash
python main.py
```

若看到以下訊息，代表 Finnhub 正常運作：

```
[步驟 1.5] 確保白名單股票（NVDA 等）必定出現
  [OK] 白名單股票全部存在 (17 檔)
```

或

```
  [!] 發現 3 檔白名單股票缺失: NVDA, AMD, TSM
  [*] 正在補抓白名單股票...
    補抓 NVDA... [OK]
    補抓 AMD... [OK]
```

---

## 費用限制

| 方案 | API Calls | 費用 |
|------|-----------|------|
| **免費** | 60/分鐘 | $0 |
| Standard | 300/分鐘 | $9/月 |
| Pro | 600/分鐘 | $99/月 |

**Alpha Finder 每日使用量**：
- 白名單補抓：~17 calls
- 財報日期查詢：~60 calls
- 目標價查詢：~60 calls
- **合計**：~140 calls（遠低於免費額度 3600/小時）

---

## 常見問題

### Q: 可以不填 API Key 嗎？

**可以**。系統會 fallback 到純 yfinance 模式，但可能：
- 缺少某些財報日期
- 白名單股票（NVDA）可能不出現

### Q: API Key 會過期嗎？

不會。Finnhub 免費方案的 Key 永久有效，除非你刪除帳號。

### Q: 如何檢查 API 使用量？

登入 Finnhub Dashboard 查看：https://finnhub.io/dashboard
- 顯示當日已使用的 API calls
- 免費方案可看到剩餘額度

---

## 🎯 建議

✅ **強烈建議填寫 API Key**
- 花 2 分鐘註冊
- 確保 NVDA 等龍頭股不會漏掉
- 財報日期更準確

生效後，全量數據就會包含 NVDA 了！🚀
