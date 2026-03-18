# ai_decision_contract_v2.md

## 0. 文件定位

本文件定義 Alpha Sniper 最終單一輸出 `ai_decision_YYYY-MM-DD.csv` 的官方欄位契約。
本版採用「內部雙引擎、外部單輸出」設計。

說明：
- 內部雙引擎 = continuation + sniper
- 外部單輸出 = 使用者最終只看一份 ai_decision
- 最終 `ai_decision_YYYY-MM-DD.csv` 原則上由 Web AI 依 `bundle + Protocol` 產出；工程端只提供資料、候選、模板與契約檢查，不直接拍板 final。

---

## 1. 輸出目標

最終 ai_decision 只回答五件事：

1. 現在最值得先看誰
2. 這筆屬於 continuation 還是 sniper
3. 為什麼是它
4. 怎麼進
5. 什麼情況下失效

---

## 2. 檔名規範

輸出檔名：
`ai_decision_YYYY-MM-DD.csv`

例如：
`ai_decision_2026-03-18.csv`

---

## 3. 一檔股票一列

每列代表一個可執行決策候選。
同一 ticker 在同一天原則上只保留一列最終版本。

若同一 ticker 同時出現在 continuation 與 sniper：
- 允許內部同時存在
- 最終輸出只保留優先級較高的那一列
- 必須在 `decision_mode`、`decision_reason` 中說清楚保留原因

---

## 4. 必要欄位

### 4.1 識別欄位
- as_of_date
- ticker
- company_name
- decision_mode
- final_priority
- decision_status

### 4.2 核心判斷欄位
- decision_score
- decision_reason
- primary_event_type
- trigger_source
- trigger_score
- continuation_rank
- tomorrow_continuation_prob
- confidence_tier

### 4.3 執行欄位
- entry_plan
- execution_window
- avoid_chase_flag
- preferred_entry_type
- vwap_status
- sqzmom_status
- volume_status

### 4.4 風險欄位
- invalidation_rule
- risk_level
- risk_note
- dilution_flag
- halt_risk_flag

### 4.5 追蹤欄位
- source_sheet_trace
- protocol_version
- data_version
- decision_ts

---

## 5. 欄位定義

### as_of_date
決策所屬交易日。

### ticker
股票代碼。

### company_name
公司名稱，可空，但建議保留。

### decision_mode
決策模式，只允許以下兩值：
- continuation
- sniper

### final_priority
最終優先順序，1 為最優先。

### decision_status
只允許以下四值：
- ready
- watch
- avoid_chase
- invalid

### decision_score
最終總決策分數，用於單表排序，不要求對外解釋精確公式，但必須穩定。

### decision_reason
一句話說清楚為何入選。
禁止只寫「技術面偏多」這種廢話。

### primary_event_type
若為 sniper，必填。
若為 continuation，可空。

### trigger_source
若為 sniper，填事件來源，如 sec_8k / earnings_release / analyst / news。
若為 continuation，可空。

### trigger_score
若為 sniper，必填。
若為 continuation，可空。

### continuation_rank
若為 continuation，填原本 continuation 引擎排名。
若為 sniper，可空。

### tomorrow_continuation_prob
若為 continuation，填隔日延續機率。
若為 sniper，可空或填 null。

### confidence_tier
只允許：
- A
- B
- C

### entry_plan
直接寫人話，例如：
- 開盤不追，等第一次回踩 VWAP 再看
- 事件後 2 分鐘內若守 VWAP 可試單
- 若直接跳空過大則不追

### execution_window
只允許：
- premarket
- open_0_15m
- open_15_60m
- intraday
- next_day_only

### avoid_chase_flag
布林值。
TRUE = 不追價
FALSE = 可依條件執行

### preferred_entry_type
只允許：
- vwap_reclaim
- first_pullback
- breakout_retest
- open_drive
- no_trade

### vwap_status
只允許：
- above
- reclaiming
- below
- unknown

### sqzmom_status
只允許：
- positive
- flipping_up
- negative
- unknown

### volume_status
只允許：
- expanding
- stable
- weak
- unknown

### invalidation_rule
必填，且必須能執行。
例如：
- 回踩 VWAP 失守且量縮反彈無力
- 補充文件顯示 ATM / offering
- 1m 與 5m SQZMOM 同步翻負

### risk_level
只允許：
- low
- medium
- high

### risk_note
用一句話補充真正風險來源。
例如：
- 超小市值高波動
- 稀釋風險未完全排除
- 已大幅脫離事件價

### dilution_flag
布林值。

### halt_risk_flag
布林值。

### source_sheet_trace
記錄來源，例如：
- decision_signals_daily|monster_radar_daily
- live_event_feed|event_score_log|trade_trigger_queue

### protocol_version
例如：
- continuation_protocol_v1
- event_sniper_protocol_v1
- hybrid_decision_v2

### data_version
bundle 或 feed 的版本字串。

### decision_ts
實際產出決策的時間戳。

---

## 6. 排序規則

最終 ai_decision 必須是單一排序表。
排序主邏輯如下：

1. 先過濾 invalid
2. 再比較是否具備可執行 entry_plan
3. 再比較 decision_score
4. 若分數接近，優先保留硬催化明確、且尚未過度擴張者
5. 若已嚴重脫離可執行區，降級為 watch 或 avoid_chase

---

## 7. 決策模式說明

### continuation
適用於：
- 已有日線強勢
- 隔日延續機率高
- 可接受等回踩再切入

### sniper
適用於：
- 剛出硬催化
- 需要更快決策
- 進場窗口短
- 更重視事件真實性與價格擴張程度

---

## 8. 最小輸出範例

欄位：
as_of_date,ticker,company_name,decision_mode,final_priority,decision_status,decision_score,decision_reason,primary_event_type,trigger_source,trigger_score,continuation_rank,tomorrow_continuation_prob,confidence_tier,entry_plan,execution_window,avoid_chase_flag,preferred_entry_type,vwap_status,sqzmom_status,volume_status,invalidation_rule,risk_level,risk_note,dilution_flag,halt_risk_flag,source_sheet_trace,protocol_version,data_version,decision_ts

範例 1：
2026-03-18,ABCD,ABCD Inc,sniper,1,ready,91,"8-K 合約硬催化且價格尚未過度擴張",major_contract,sec_8k,10,,,"A","事件後第一次回踩 VWAP 不破可試單",open_0_15m,FALSE,first_pullback,reclaiming,flipping_up,expanding,"跌回 VWAP 下且 5m SQZMOM 翻負",high,"小市值高波動",FALSE,TRUE,"live_event_feed|event_score_log|trade_trigger_queue",event_sniper_protocol_v1,bundle_20260318_01,2026-03-18T20:31:00Z

範例 2：
2026-03-18,EFGH,EFGH Corp,continuation,2,watch,83,"日線延續結構完整但需等回踩確認",, , ,1,74.5,"B","開盤不追，等回踩 1-2% 觀察",next_day_only,TRUE,first_pullback,above,positive,stable,"回踩後量縮且守不住 VWAP",medium,"已脫離舒適買點",FALSE,FALSE,"decision_signals_daily|monster_radar_daily",hybrid_decision_v2,bundle_20260318_01,2026-03-18T20:31:00Z

---

## 9. 禁止事項

- 不可輸出沒有 invalidation_rule 的決策
- 不可把 continuation 偽裝成 sniper
- 不可把 rumor 直接列為 ready
- 不可同一天對同一 ticker 輸出兩列互相矛盾的 final decision
- 不可讓 preview 結果覆蓋 final 結果

---

## 10. 一句話原則

最終只交一份 ai_decision，
但那份表裡的每一筆決策都必須清楚告訴我：
它是續強，還是事件狙擊；
能不能打，為什麼打，什麼時候該退。
