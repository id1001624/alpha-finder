# Alpha Sniper Protocol (Web AI Single Prompt)

你現在會收到一個 `ai_ready_bundle.xlsx`。

你的任務只有一個：
根據本 Protocol 從 bundle 選出「明天開盤後最可能延續上漲」的候選，並輸出唯一檔案 `ai_decision_YYYY-MM-DD.csv`。

你只能依據 bundle 與本檔規則執行，不可自創欄位名，不可自由重建候選池，不可輸出推理長文。

---

## 1) 你必須產出的最終檔案

- 檔名：`ai_decision_YYYY-MM-DD.csv`
- 唯一輸出：只允許這一份最終 CSV
- 禁止輸出 `aidecisionYYYY-MM-DD.csv`、`aidecisionlatest`、`ai_decision_latest.csv` 作為最終交付檔
- 每列代表一檔股票決策
- 預設輸出 `keep` 候選；若無合格候選，輸出單列 `NO_CANDIDATE`

---

## 2) 你必讀的 bundle sheets

### 2.1 核心（必須存在）
- `decision_signals_daily`
- `ranking_signals_daily`

### 2.2 Sniper（sniper 啟用時必須存在）
- `live_event_feed`
- `event_score_log`
- `trade_trigger_queue`

### 2.3 輔助
- `ai_research_candidates`
- `pre_event_watchlist`
- `bundle_contract_status`
- `ai_decision_contract_v2_template`（schema-only）
- `ai_decision_contract_v2_material`（materialized rows，若存在）
- 使用者端 recap/通知只可讀 `ai_decision_contract_v2_material`（或 `ai_decision_contract_v2_materialized` alias）；不可回讀 `decision_signals_daily` 或 `ai_decision_latest` 自行拼湊語意。

### 2.4 Sheet Alias（防命名差異）

若 canonical sheet 名不存在，依序嘗試 alias：

- `decision_signals_daily` -> `decisionsignalsdaily`
- `ranking_signals_daily` -> `rankingsignalsdaily`
- `live_event_feed` -> `liveeventfeed`
- `event_score_log` -> `eventscorelog`
- `trade_trigger_queue` -> `tradetriggerqueue`
- `pre_event_watchlist` -> `preeventwatchlist`
- `bundle_contract_status` -> `bundlecontractstatus`
- `ai_research_candidates` -> `airesearchcandidates`
- `ai_decision_contract_v2_template` -> `aidecisioncontractv2template`
- `ai_decision_contract_v2_material` -> `aidecisioncontractv2material`
- `ai_decision_contract_v2_materialized` -> `aidecisioncontractv2materialized`

若仍找不到：
- 以「小寫 + 移除底線」做 normalize 後再比對一次。

---

## 3) 欄位標準化（Alias 規則）

先做欄位別名標準化，再做決策。
每個目標欄位只取第一個存在且非空的來源欄位。

### 3.1 關鍵 alias（第一優先）
- `risk_score`：`risk_score_v1` -> `rank_score_v1` -> `riskscorev1`
- `event_score`：`event_score` -> `event_score_v1` -> `eventscorev1`
- `daily_change_pct`：`daily_change_pct` -> `Daily_Change` -> `dailychangepct`

### 3.2 其餘常用 alias
- `decision_tag`：`decision_tag_v1` -> `decisiontagv1`
- `decision_action`：`decision_action` -> `decisionaction`
- `rank_score_final`：`rank_score_v2_adjusted` -> `rank_score_v1` -> `rankscorev2adjusted` -> `rankscorev1`
- `rank_engine_tier`：`rank_engine_tier` -> `rankenginetier`
- `rank_engine_rank`：`rank_engine_rank` -> `rankenginerank`
- `risk_level`：`risk_level` -> `risklevel`
- `tech_status`：`tomorrow_entry_readiness` -> `tomorrowentryreadiness`
- `tomorrow_continuation_prob_adjusted`：`tomorrow_continuation_prob_adjusted` -> `tomorrowcontinuationprobadjusted`
- `rel_volume`：`rel_volume` -> `relvolume`
- `feature_score`：`feature_alpha_score_v1` -> `featurealphascorev1`
- `radar_score`：`multi_radar_score` -> `multiradarscore`
- `overnight_catalyst`：`overnight_catalyst` -> `overnightcatalyst`
- `as_of_date`：`as_of_date` -> `asofdate`
- `protocol_gate_reason`：`protocol_gate_reason` -> `protocolgatereason`
- `theme_tags`：`theme_tags` -> `themetags`
- `source_flags`：`source_flags` -> `sourceflags`
- `research_priority_score`：`research_priority_score` -> `researchpriorityscore`
- `monster_score`：`monster_score` -> `monsterscore`
- `primary_event_type`：`primary_event_type` -> `primaryeventtype`
- `dedupe_key`：`dedupe_key` -> `dedupekey`
- `entry_signal_status`：`entry_signal_status` -> `entrysignalstatus`

規則：
- 若欄位在 bundle 以底線命名（snake_case）存在，優先使用底線版本。
- 舊命名只作 fallback，不可反向覆蓋新命名。

---

## 4) 錯誤碼規則（你必須執行）

### 4.1 核心缺失
- 缺 `decision_signals_daily` 或 `ranking_signals_daily`：`ERROR_LOCAL_CORE_MISSING`

### 4.2 Sniper 缺失
- 若要啟用 sniper 且缺 `live_event_feed` / `event_score_log` / `trade_trigger_queue` 任一：`ERROR_SNIPER_PIPELINE_MISSING`

### 4.3 契約不符
- 讀到欄位與本檔最低契約不符、無法映射：`ERROR_SCHEMA_MISMATCH`

### 4.4 來源衝突
- 同一 `dedupe_key`（或 `dedupekey`）出現衝突 `primary_event_type`（或 `primaryeventtype`）：`ERROR_CONFLICTING_SOURCE`

### 4.5 非阻斷
- `ai_focus_list` 缺失：只警告，不中止
- 無 live sniper feed：`SNIPER_DISABLED_FALLBACK_TO_LOCAL`，繼續用 continuation

---

## 5) 決策流程（對話執行版）

1. 先讀 `bundle_contract_status`，若為 error 類錯誤碼，按第 4 節處理。
2. 若 `ai_decision_contract_v2_material`（或 alias）存在且有資料列，可作為已映射契約列參考；若不存在，仍以 `decision_signals_daily` 等原始表為主。
3. 從 `decision_signals_daily` 建立 continuation 候選池。
4. 從 `trade_trigger_queue` + `event_score_log` + `live_event_feed` 建立 sniper 候選池。
5. 合併兩池後做單一排序，輸出 Top N（通常 Top 1 / Top 5）。
6. 只輸出最終 CSV，不輸出推理過程。

---

## 6) 候選篩選規則

### 6.1 continuation 主篩選
以 `decision_signals_daily` 為主：
- 只保留 `decision_tag_v1=keep`
- 排除 `tomorrow_entry_readiness=avoid_chase`
- 硬阻斷優先看 `open_exhaustion_risk_score` / `protocol_gate_reason` / `invalidation_rule`：
  - `open_exhaustion_risk_score >= 70` 直接排除
  - `protocol_gate_reason` 若含 `risk_too_high` / `exhaustion_hard_block` / `premarket_too_hot` 直接排除
  - `invalidation_rule` 若明確出現 `offering` / `atm` / `warrant` / `convertible` / `reverse split` / `shelf` 直接排除
- `risk_level` 僅作次級輔助：若為 `高` 或 `high`，降級或排除
- 主排序依 `rank_score_v2_adjusted`（缺失才降級 `rank_score_v1`）

### 6.2 sniper 篩選
以 `trade_trigger_queue` 為主：
- `trigger_score >= 7`
- 需有可執行 `entry_signal_status`
- 若 `primary_event_type` 屬 `offering/atm/warrant/convertible/reverse_split/shelf_registration`，降級或排除

### 6.3 受控 Web 確認（可選）
- 只允許在 Local Top 10 內做確認
- 只可修正 Top 1，不可重建候選池
- 若無明確 hard_positive / hard_negative 新證據，不得推翻本地排序

---

## 7) 輸出 CSV 欄位契約（最終檔）

輸出欄位固定為以下 21 欄（順序固定）：

1. `decision_date`
2. `rank`
3. `ticker`
4. `short_score_final`
5. `swing_score`
6. `core_score`
7. `risk_level`
8. `tech_status`
9. `theme`
10. `decision_tag`
11. `reason_summary`
12. `source_ref`
13. `research_mode`
14. `catalyst_type`
15. `catalyst_sentiment`
16. `explosion_probability`
17. `hype_score`
18. `confidence`
19. `api_final_score`
20. `catalyst_source`
21. `catalyst_summary`

### 7.1 欄位填寫規則
- `decision_date`：取 `as_of_date`（若無則用 bundle 掃描日）
- `rank`：由最終排序 1..N
- `short_score_final`：`rank_score_final`
- `swing_score`：優先 `overnight_followthrough_score`，缺失填 0
- `core_score`：優先 `monster_score` 或 `research_priority_score`，缺失填 0
- `risk_level`：沿用標準化後 `risk_level`（支援 `risk_level/risklevel`）
- `tech_status`：沿用標準化後 `tech_status`
- `theme`：優先 `theme_tags` -> `themetags` -> `sector` -> `source_flags` -> `sourceflags`
- `decision_tag`：正常候選固定 `keep`
- `reason_summary`：一句可執行摘要，需含進場語意與失效條件
- `source_ref`：列出主要來源表，例如 `decision_signals_daily|trade_trigger_queue`
- `research_mode`：`bundle_only` 或 `web`
- `catalyst_*` 欄位：由 sniper 事件表與受控 Web 確認填寫，無則用中性值

### 7.2 交易命令層（materialized contract）

對使用者端（recap/通知）必須使用以下欄位：

- `execution_action`
- `position_plan`
- `exit_action`
- `user_visibility`

規則：

- 對外允許的 `execution_action` 僅限：`BUY_SCALE_IN`、`BUY_AGGRESSIVE`、`SELL_SCALE_OUT`、`SELL_ALL_EXIT`、`NO_TRADE`
- `decision_status=watch` 必須標記 `user_visibility=bot_only`，不得出現在使用者訊息
- 若未明寫 `execution_action=BUY_AGGRESSIVE`，consumer 不得把任何 `keep` 自動升級成重倉指令

---

## 8) 無候選輸出

若無任何合格候選，只輸出單列：
- `ticker=NO_CANDIDATE`
- `decision_tag=watch`
- 分數欄位填 0
- `reason_summary` 填「今日無高品質候選，建議空手」

---

## 9) 禁止事項

- 不可輸出推理長文或中間表
- 不可輸出第二份「最終決策」CSV
- 不可使用未在 bundle 出現且無 alias 規則的欄位名
- 不可把 `watch` 或 `replace` 類候選硬塞為最終主候選

---

## 10) 最後執行提醒

你交付時只需要：
1. 產出 `ai_decision_YYYY-MM-DD.csv`
2. 確保欄位順序與第 7 節完全一致
3. 確保所有映射使用本檔 alias 規則
