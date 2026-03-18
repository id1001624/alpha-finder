# event_sniper_protocol.md

## 0. 文件定位

本文件是 Alpha Sniper 專案的官方事件狙擊規格。
目的不是取代既有 continuation 決策流程，而是補上「即時硬催化先發現、再用技術確認開槍」的能力。

本文件同時約束：
1. 我在 AI 端的判讀方式
2. 資料層的欄位契約
3. 最終輸出到 ai_decision 的標準格式

本文件優先級高於一般備忘錄、臨時口頭規則、散落在程式中的隱含假設。

---

## 1. 核心目標

本模組唯一任務是：

- 在市場尚未全面擴散前
- 盡可能早地抓到「剛出硬催化」的股票
- 再用 VWAP + SQZMOM 做執行確認
- 最後把可交易候選送進單一 ai_decision 輸出

本模組不是：
- 日線續強排序器
- 長線基本面研究器
- 盤後回顧器
- 純新聞摘要器

---

## 2. 核心原則

### 2.1 事件先行
先有事件，再做技術確認。
沒有事件的純日線強勢股，不屬於 event sniper 主流程。

### 2.2 技術不預測，只確認
VWAP、SQZMOM、量能不是第一層發現器，而是第二層扣板機。
事件剛出來但技術失敗，可以不進。
技術再漂亮但沒有硬事件，不算 sniper。

### 2.3 先抓早，不是先求穩
本模組容許高波動、高淘汰率。
目標不是提高勝率到最漂亮，而是提高「抓到首波主升段」的能力。

### 2.4 稀釋直接重罰
offering、ATM、warrants、convertible、reverse split 屬於高優先負面事件。
若事件本身帶有明顯融資稀釋性，原則上不得列為 sniper buy。

---

## 3. 資料來源

### 3.1 必要來源
至少接入以下任兩類，理想是三類全接：

- 即時新聞流
- 即時 SEC / 8-K / 10-Q / 10-K / S-3 / 424B / Form 4 流
- analyst / earnings / target revision 流

### 3.2 資料來源分級
- Tier A：SEC filing、8-K、正式財報稿、FDA、政府/軍工/大型客戶合約
- Tier B：analyst upgrade、target raise、confirmed earnings update、產業級重要新聞
- Tier C：一般 PR、媒體整理、二手轉述、社群轉貼

### 3.3 時效要求
event sniper 使用的資料源必須盡可能接近即時。
免費延遲源只能作備援，不可作主發現源。

---

## 4. 事件分類

所有 headline / filing 進入系統後，必須先被標準化成以下 event_type 之一：

- earnings_beat
- guidance_raise
- guidance_init
- major_contract
- major_customer
- fda_update
- analyst_upgrade
- analyst_target_raise
- sec_8k_positive
- sec_10q_positive
- sec_10k_positive
- insider_buy
- theme_breakout
- rumor_unverified
- offering
- atm_program
- warrant
- convertible
- reverse_split
- shelf_registration
- dilution_other
- neutral_other

若同一事件符合多類，允許多標籤，但必須指定 primary_event_type。

---

## 5. 事件打分

### 5.1 公式
TriggerScore = SourceScore + CatalystScore + ThemeScore + PreEarnScore + MarketStructureScore - DilutionRisk - NoisePenalty

### 5.2 SourceScore
- SEC / 8-K / 正式財報稿 / FDA / 政府公告 = 4
- analyst action / confirmed calendar update = 2
- 一般公司 PR = 1
- 二手媒體轉述 = 1
- 未驗證 rumor = 0

### 5.3 CatalystScore
- guidance raise = 4
- revenue / EPS 明顯 beat = 4
- major contract / major customer / hyperscaler / NVIDIA / Microsoft / defense = 3
- new product launch with hard commercial signal = 2
- 52 週新高 ahead of event = 2
- 模糊敘事、沒有硬數字 = 0 到 1

### 5.4 ThemeScore
- AI / data center / optical / 800G / defense / satellite / crypto infra = 1 到 2
- 非主流弱題材 = 0 到 1

### 5.5 PreEarnScore
- T+1 ~ T+14 有財報 = 2
- 明確 BMO / AMC = 1
- 無相關 = 0

### 5.6 MarketStructureScore
- 小市值且流動性足夠 = 1 到 2
- 浮動股本偏緊 = 1
- 盤前已有異常量價但尚未完全擴散 = 1
- 已大幅脫離事件前價格 = 0

### 5.7 DilutionRisk
- offering / ATM / warrants = 6
- reverse split = 6
- convertible = 4
- shelf registration = 3
- 其他稀釋事件 = 2 到 5

### 5.8 NoisePenalty
- 重複新聞、低品質聚合、社群轉發噪音 = 1 到 3
- headline 與 filing 本體不一致 = 3 到 5

---

## 6. 第一層過濾

進入技術確認前，必須先通過以下條件：

1. TriggerScore >= 7
2. price > 1.0 美元
3. 有基本流動性，不可完全無法成交
4. 非明確稀釋主題
5. 非純 rumor
6. 事件時間戳必須可追蹤

若 TriggerScore >= 9，可列入高優先事件候選。
若 TriggerScore 7~8，列入一般候選。
若 TriggerScore <= 6，原則上不進 sniper queue。

---

## 7. 第二層技術扣板機

通過第一層後，不直接買入，必須再經過技術確認。

### 7.1 可進場條件
以下條件至少滿足 2 項，且第 1 項必須成立：

1. 價格站上或重新收復 VWAP
2. 第一次回踩 VWAP 不破
3. 1m 或 5m SQZMOM 翻正
4. 1m / 5m 成交量明顯放大
5. 新聞後漲幅尚未相對事件前基準價擴張超過 6%

### 7.2 避免追價條件
以下任一成立，標記 avoid_chase：

- 事件後 2 分鐘內已脫離基準價超過 8%
- 第一段直線拉升後無回踩
- 1m 結構過度擴張且量價背離
- headline 看起來很強，但 filing 細節偏空或含稀釋

### 7.3 直接失效條件
以下任一成立，直接取消：

- 回落並有效跌破 VWAP
- SQZMOM 快速翻負且量縮
- 新出補充文件顯示 offering / ATM / warrant / reverse split
- 原始 headline 被更正、撤稿、或證實誤讀

---

## 8. 必要資料表

### 8.1 pre_event_watchlist
用途：盤前預熱名單

必要欄位：
- ticker
- event_date
- days_to_event
- earnings_session
- sector
- theme_tags
- prior_gap_profile
- analyst_count
- target_price
- upside_pct
- float_proxy
- market_cap
- liquidity_proxy
- pre_event_score
- watch_reason
- source_ts

### 8.2 live_event_feed
用途：即時事件原始流

必要欄位：
- event_id
- ts
- ticker
- source_name
- source_tier
- headline
- url
- event_type_raw
- primary_event_type
- sentiment_raw
- price_at_event
- market_cap
- session
- dedupe_key

### 8.3 event_score_log
用途：事件拆分與打分

必要欄位：
- event_id
- ticker
- source_score
- catalyst_score
- theme_score
- preearn_score
- market_structure_score
- dilution_risk
- noise_penalty
- trigger_score
- score_reason
- high_priority_flag
- scoring_version
- ts

### 8.4 trade_trigger_queue
用途：待執行候選池

必要欄位：
- event_id
- ticker
- trigger_score
- decision_mode
- trigger_source
- primary_event_type
- price_at_event
- current_price
- price_extension_pct
- relvol_1m
- relvol_5m
- vwap_dist
- sqzmom_1m
- sqzmom_5m
- entry_signal_status
- execution_window
- invalidate_if
- queue_rank
- ts

---

## 9. 與 continuation 的關係

event sniper 與 continuation 是兩條不同判斷線。

- continuation 回答：明天誰最可能延續上漲
- event sniper 回答：現在誰因硬事件值得立刻盯與準備出手

兩條線都可以輸入最終 ai_decision。
但輸出給使用者時，原則上維持單一 ai_decision，只用 decision_mode 區分來源。

---

## 10. 最終輸出規則

若某檔股票來自 event sniper，輸出時必須帶：

- decision_mode = sniper
- trigger_source
- primary_event_type
- trigger_score
- execution_window
- entry_plan
- invalidation_rule

若某檔股票來自 continuation，輸出時必須帶：

- decision_mode = continuation
- continuation_rank
- tomorrow_continuation_prob
- entry_plan
- invalidation_rule

同一天若兩條線都有候選，最終只做一次總排序。
排序不是看單一分數最大，而是綜合：
- 是否屬於硬催化
- 是否仍有入場空間
- 技術結構是否可執行
- 失效是否清楚
- 是否已過度擴張

---

## 11. AI 決策責任切分

我負責：
- 根據 bundle + protocol 做最終判讀
- 給出 Top 1 / Top 5
- 說明 entry_plan 與 invalidation_rule
- 在 continuation 與 sniper 候選中選出最值得先看的標的

資料層負責：
- 穩定產出 sheet
- 維持欄位名與語意一致
- 確保時間戳、事件來源、打分過程可回溯
- 不得自行把 preview 當 final decision

---

## 12. 禁止事項

- 不可把純日線強勢股偽裝成 sniper
- 不可把未驗證 rumor 當硬事件
- 不可忽略 offering / ATM / warrant 等稀釋訊號
- 不可用單一 headline 直接下結論，需優先看原始來源
- 不可讓 recap 或保守規則把明確高分 sniper 候選全部過濾掉

---

## 13. 一句話原則

先抓硬事件，再用技術確認；
寧可高淘汰，也不要永遠只在大漲後才看到它。
