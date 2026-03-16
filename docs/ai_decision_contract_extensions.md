# AI Decision Contract Extensions

本文件記錄近期新增欄位與行為保證，避免下游讀取破版。

## 1) Overnight Catalyst Contract

來源檔案：`repo_outputs/ai_trading/latest/overnight_catalyst_check.csv`

欄位：
- `ticker`
- `catalyst_time`
- `catalyst_type`
- `impact`
- `sentiment_score`
- `sentiment_confidence`
- `final_impact`
- `source_snippet`

行為保證：
- `impact` 由新聞分類而來。
- `final_impact` 由 `impact` 與 sentiment 融合後得到。
- 下游排序與決策優先使用 `final_impact`，若缺失才回退 `impact`。

## 2) Decision Signals Contract

來源檔案：`repo_outputs/ai_trading/latest/decision_signals_daily.csv`

新增/重點欄位：
- `rank_score_v2_adjusted`
- `overnight_catalyst`
- `setup_type`
- `tomorrow_entry_readiness`
- `tomorrow_continuation_prob_adjusted`

行為保證：
- `rank_score_v2_adjusted` 會反映 overnight catalyst 的加減權重。
- `tomorrow_entry_readiness` 僅使用固定枚舉：`ignition_ready` / `pullback_watch` / `avoid_chase` / `neutral`。
- `tomorrow_continuation_prob_adjusted` 範圍固定在 0 到 99。

## 3) Recap Contract

recap payload 新增：
- `swing_strategy_recommendation`

行為保證：
- `mode=bedtime` 時會嘗試產生 swing 建議。
- 若 native QuantMuse 不可用，會回退 rule-based 建議，不中斷 recap。

## 4) Similar Trade Memory Contract

使用位置：
- `intraday_execution_engine` snapshot payload
- `watchlist_brief` engine payload

欄位：
- `similar_past_trades` (list)

每個元素結構：
- `ticker`
- `action`
- `signal_type`
- `outcome`
- `similarity`
- `reason_summary`
- `recorded_at`

行為保證：
- 若查無可用歷史或相似度不足，輸出空陣列 `[]`。
- 不會因 trade memory 缺失中斷主流程。
