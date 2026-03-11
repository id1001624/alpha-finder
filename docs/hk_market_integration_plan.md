# 港股接入計畫

## 目標

- 在不破壞現有美股主流程的前提下，評估是否新增 HK 市場模式
- 保持 ai_decision 契約穩定，不把美股與港股硬混成同一套時段與規則
- 優先做最小可行版本，再決定要不要擴到完整 intraday / Discord / recap 支援

## 現況限制

1. ticker 正規化目前偏美股，港股常見代碼如 0700.HK、9988.HK 會在資料管線被擋掉
2. intraday active window、opening recap、premarket wording 都是美股假設
3. watchlist 新聞 query 現在固定使用美股語境，港股需要改成 exchange-aware query
4. repo 目前沒有 HK 專用交易時段模型，尚未處理午休、不同開盤時間與半日市
5. ranking / risk / catalyst 欄位目前以美股 scanner 與美股分鐘資料源為核心

## 建議分階段

### Phase 1: 資料層可接入

- 放寬 ticker normalize，允許數字開頭與 .HK 後綴
- 在 market dataset / ai_ready bundle 中保留 exchange 欄位，不再只靠 ticker 猜市場
- 確認 yfinance / Yahoo chart 對港股分鐘資料的穩定度
- 決定港股是否需要獨立 universe 篩選規則

### Phase 2: 市場時段抽象化

- 把 intraday active window 抽成 market-aware session helper
- 新增 HK session 設定：timezone、open、lunch break、close
- watchlist 新鮮度判斷改成依 market session，而不是只看美股 active window
- opening / morning / bedtime recap 的 market open 參考時間改成依 market mode 決定

### Phase 3: 策略與排序拆分

- 檢查美股專用條件：premarket、earnings、float rotation、monster radar 是否直接可沿用
- 若不可沿用，建立 HK profile，避免共用同一組閾值
- watchlist 的新聞 query 改成 market-aware wording
- Gemini prompt 中加入 market context，避免把港股當美股解讀

### Phase 4: Discord 與 operator flow

- 決定 /watchlist 是否需要指定市場，例如 `/watchlist --market hk 0700.HK 9988.HK`
- 決定 ai_decision 是否拆成 US / HK 兩份，或維持單檔多市場欄位
- README 與 Protocol 明確標示哪些流程支援 US only、哪些支援 HK

## 最小可行版本

若只想先試港股，不建議一開始就做完整盤中 execution。最小可行版本建議是：

1. 先讓 daily dataset / ai_decision 可以合法保留 HK ticker
2. 先讓 `/watchlist` 能讀取 HK ticker 並做新聞與持倉整合
3. 先不承諾 HK intraday engine / opening recap / auto execution

## 不建議現在做的事

- 不要直接把 HK ticker 混進現有美股 scanner 門檻
- 不要把午休市場硬塞進目前單段 active window
- 不要在還沒拆市場規則前，就讓 web AI 同時輸出 US / HK 混合排序

## 需要改動的主要檔案

- config.py
- ai_trading/contracts.py
- ai_trading/market_data_pipeline.py
- ai_trading/watchlist_brief.py
- ai_trading/intraday_execution_engine.py
- scripts/run_intraday_execution_engine.py
- scripts/push_alerts_from_ai_decision.py
- Alpha-Sniper-Protocol.md
- README.md

## 建議決策順序

1. 先決定港股只做 research/watchlist，還是要做到 intraday execution
2. 再決定 ai_decision 要單市場還是多市場契約
3. 最後才改 scanner / recap / Discord 命令介面