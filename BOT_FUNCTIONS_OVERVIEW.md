# Alpha Finder Bot 功能與策略說明（現況）

更新日期：2026-03-12

這份文件是目前 alpha-finder 專案的實作現況整理，重點回答：
- 我們 bot 現在有哪些功能
- 指標用了什麼、怎麼算
- 進場 / 平倉 / 加碼 / 減碼策略是什麼
- engine 與 recap 現在怎麼工作
- Tavily + Gemini 在哪裡被使用

## 1) Bot 目前功能（Discord 操作面）

主要入口是 scripts/run_discord_trade_bot.py。

目前核心功能：
- 手動成交回報：/buy、/add、/sell
- 持倉查詢：/positions、/position
- 成交與 execution 歷史查詢：/trades、/executions（來源優先 Turso）
- 綜合觀察卡：/watchlist
- 保存關注股管理：/watchadd、/watchremove、/watchsaved
- 指令說明：/tradehelp

成交回報資料流：
- /buy /add /sell 會更新持倉（positions_latest）
- 同步寫入成交 ledger（position_trade_log）
- 後續 intraday engine / recap 會直接沿用這份最新狀態

## 2) 我們現在的指標與計算方式

指標實作在 ai_trading/intraday_indicators.py，主體是兩個：
- SQZMOM（LazyBear 風格）
- Dynamic Swing AVWAP

### 2.1 SQZMOM（calc_sqzmom_lb）

計算元素：
- True Range: max(High-Low, |High-prevClose|, |Low-prevClose|)
- Keltner Channel（KC）
  - kc_basis = Close 的 kc_length 均線
  - ATR = TR 的 kc_length 均值
  - upper_kc = kc_basis + kc_mult * ATR
  - lower_kc = kc_basis - kc_mult * ATR
- Bollinger Bands（BB）
  - bb_basis = Close 的 bb_length 均線
  - std = Close 的 bb_length 標準差
  - upper_bb = bb_basis + bb_mult * std
  - lower_bb = bb_basis - bb_mult * std

狀態判斷：
- sqz_on: lower_bb > lower_kc 且 upper_bb < upper_kc
- sqz_release: 前一根在 sqz_on、當前不在 sqz_on

動能柱（sqzmom_hist）：
- 先算 avg_price = ((hh + ll) / 2 + kc_basis) / 2
- delta = Close - avg_price
- 用一組線性權重對 delta 做 rolling dot product 形成 hist
- sqzmom_delta = hist.diff()

顏色規則（sqzmom_color）：
- lime: hist >= 0 且 delta >= 0
- green: hist >= 0 且 delta < 0
- red: hist < 0 且 delta < 0
- maroon: hist < 0 且 delta >= 0

### 2.2 Dynamic Swing AVWAP（calc_dynamic_swing_avwap）

核心概念：
- 用 swing high / swing low 當錨點重置 AVWAP
- 用 ATR / 平均 ATR 的波動比率，動態調整衰減速度（alpha）

流程摘要：
- 偵測 swing high / swing low（以 swing_period 視窗）
- 出現新 swing 且方向切換時，重置 sum_pv / sum_v（等於新錨點）
- 非新錨點時，使用 alpha 做遞減加權更新：
  - sum_pv = sum_pv * (1-alpha) + tp * vol
  - sum_v = sum_v * (1-alpha) + vol
- dynamic_avwap = sum_pv / sum_v

### 2.3 engine 使用到的派生欄位

add_intraday_indicators 會補：
- above_avwap / below_avwap
- sqzmom_positive / sqzmom_rising / sqzmom_falling
- long_trigger = sqz_release 且 sqzmom_positive 且 above_avwap

## 3) 目前策略邏輯（進場 / 平倉 / 加碼 / 減碼）

策略主體在 ai_trading/intraday_execution_engine.py 的 _classify_action。

### 3.1 先決條件（目前預設為 monster_swing 模式）

新倉 entry 前會先檢查：
- decision_tag 必須是 keep
- rank <= INTRADAY_ENTRY_MAX_RANK（預設 1）
- risk_level 不能是 高
- confidence >= INTRADAY_ENTRY_MIN_CONFIDENCE（預設 35）
- api_final_score >= INTRADAY_ENTRY_MIN_API_SCORE（預設 75）
- 僅允許在開盤後 entry window 內（預設 60 分鐘）
- 當日新倉數與總倉位數不能超上限
- 若啟用 INTRADAY_NO_REENTRY_SAME_DAY，當日同 ticker 不重進

### 3.2 進場（entry）條件

空手且通過門檻後，需同時成立：
- sqz_release = True
- sqzmom_hist > 0 且 hist > prev_hist（動能轉強）
- close >= avwap * (1 + INTRADAY_ENTRY_MIN_AVWAP_BUFFER_PCT/100)
- sqzmom_color in {lime, green}

建議倉位比例：
- INTRADAY_ENTRY_SIZE_FRACTION（預設 0.33）

### 3.3 全出（stop_loss）條件

有倉位時，若未實現報酬率 <= INTRADAY_HARD_STOP_LOSS_PCT（預設 -4.5%）
- 直接回傳 stop_loss
- size_fraction = 1.0（全出）

### 3.4 減碼（take_profit）條件

有倉位時，任一成立會觸發 take_profit：
- 未實現報酬 >= INTRADAY_TAKE_PROFIT_PCT（預設 6%）且 hist < prev_hist
- 不在 noise grace 內，且 close < avwap 且 hist < 0 且 color in {red, maroon}

建議減碼比例：
- INTRADAY_REDUCE_SIZE_FRACTION（預設 0.5）

### 3.5 加碼（add）條件

有倉位時，需同時成立：
- add_count < INTRADAY_MAX_ADD_COUNT（預設 1）
- 未實現報酬 >= INTRADAY_MIN_ADD_PROFIT_PCT（預設 1.5%）
- 仍在 entry window（預設開盤後 60 分鐘內）
- close >= avwap * (1 + INTRADAY_ADD_MIN_AVWAP_BUFFER_PCT/100)
- hist > 0 且 hist > prev_hist
- color in {lime, green}

建議加碼比例：
- INTRADAY_ADD_SIZE_FRACTION（預設 0.25）

### 3.6 四種輸出動作（對使用者顯示）

- entry -> 適合買
- add -> 可加碼
- take_profit -> 先減碼
- stop_loss -> 適合全出

## 4) Engine 目前怎麼工作

主程式：scripts/run_intraday_execution_engine.py -> ai_trading/intraday_execution_engine.py

### 4.1 watchlist 組成（正式 auto-trade 鏈）

engine 監控來源是：
- 今日 ai_decision Top N
- shadow watchlist（前幾日遞減保留）
- 目前持倉

注意：
- /watchadd 保存清單不直接進正式 engine 新倉判斷
- /watchadd 走獨立 watchlist follow-up recap

### 4.2 分鐘資料來源

INTRADAY_DATA_PROVIDER=auto 時：
- 若條件符合，優先 Finnhub
- 否則走 Yahoo chart API，必要時 fallback 到 yfinance

### 4.3 engine 每輪做的事

- 逐 ticker 抓分鐘 K 線
- 計算 SQZMOM + Dynamic AVWAP
- 依 _classify_action 產生 entry/add/take_profit/stop_loss
- 寫入 intraday snapshot / execution log / Turso
- 有新 action 才送 Discord 告警
- 若無新 action，按 heartbeat 週期送監控在線訊息

### 4.4 雲端排程

GitHub Actions：
- intraday monitor: .github/workflows/intraday-monitor.yml
- 以偏移分鐘排程輪詢，不靠本機常駐排程

## 5) Recap 目前怎麼工作

主程式：scripts/push_alerts_from_ai_decision.py

支援模式：
- bedtime
- morning
- opening

對應 workflow：
- .github/workflows/discord-bedtime.yml
- .github/workflows/discord-morning.yml
- .github/workflows/discord-opening.yml

### 5.1 recap 資料來源

- ai_decision latest
- positions latest
- execution log（時間窗內）
- intraday snapshot（opening validation 會用）
- 追蹤 ticker 的 Tavily 新聞（若啟用）

### 5.2 三種 recap 的角色

- bedtime：
  - 晚上收斂隔日「先做什麼」
  - 會保留 bedtime plan（供 morning/opening 參照）
- morning：
  - 針對隔夜變化重估 bedtime 劇本
  - 產生開盤 if-then 計畫
- opening：
  - 驗證 morning/bedtime 劇本在開盤首段是否成立
  - 可用 --respect-mode-window 限制只在開盤驗證窗送出

## 6) Watchlist Follow-up（獨立於正式 engine）

主程式：scripts/push_watchlist_followup.py
workflow：.github/workflows/discord-watchlist-followup.yml

目的：
- 只追蹤 /watchadd 保存名單的續強與再進場觀察
- 不把它當正式 auto-trade 新倉指令

資料來源：
- 保存清單優先讀 Turso shared state（saved_watchlists）
- 本地 JSON 僅作 fallback/migration

## 7) Tavily + Gemini 在 bot / recap 的用途

### 7.1 在 watchlist（Discord bot）

檔案：ai_trading/watchlist_brief.py

- Tavily：
  - 每個 ticker 抓短新聞 snippets（盤前催化上下文）
- Gemini：
  - 生成交易結論卡（priority_order / risk_flags / action_plan）
  - follow-up 模式也有獨立 prompt，強調「只做觀察，不等於正式買點」
- 若 Gemini 不可用：
  - 自動 fallback 到 rule-based summary

### 7.2 在 recap（bedtime/morning/opening）

檔案：scripts/push_alerts_from_ai_decision.py

- Tavily：
  - conflict ticker 新聞
  - tracked ticker 新聞
- Gemini：
  - 將 recap payload 轉成可執行、精簡的結論卡
  - morning 可在 rewrite-only 模式下只做可讀性重寫
- 若 Gemini 不可用：
  - 使用規則型 fallback lines

### 7.3 在 API 研究模式（非 Discord 指令面，但同專案）

檔案：ai_trading/catalyst_api.py

- Tavily：抓候選股新聞
- Gemini：做 catalyst 分析與 API 決策列
- 只在 AI_RESEARCH_MODE=api 路徑使用

## 8) 一句話總結目前策略風格

目前 bot + engine 是「風險優先、條件式進場、分段減碼、嚴格再進場限制」：
- 新倉極保守（Top rank + keep + 時窗 + 動能/AVWAP）
- 有倉先防守（硬停損與分段減碼）
- 加碼次數受限且只在浮盈續強發生
- watchadd 只做 follow-up，不直接推動正式 auto-trade 新倉
