# AI Trading System Spec v1（No Auto-Execution）

## 1. 目標與邊界

本系統目標是把現有 Alpha Finder 升級成 **AI Trading Intelligence Engine**，專注：

1. 妖股雷達（Radar）
2. 妖股偵測引擎（Detection）
3. 續漲/爆發預測（Prediction）

> 不包含自動下單、券商 API 執行、倉位自動管理。

---

## 2. 當前已可用資產（現有專案）

- 市場資料：Finviz + Yahoo Finance + Finnhub
- 技術訊號：TradingView webhook（VWAP / SQZ）
- 每日資料輸出：`repo_outputs/daily_refresh` + `repo_outputs/ai_ready/latest`
- AI 評估回寫：`ai_decision_YYYY-MM-DD.csv` → `ai_decision_log.csv`
- 每週制度化比較：Local / Local-Fusion / AI

---

## 3. v1 模組藍圖（分層）

### Layer A: Data Layer
- `raw_market_daily.csv`
- `xq_short_term_updated.csv`
- `theme_heat_daily.csv`
- `theme_leaders_daily.csv`
- `signals.db`（TradingView）

### Layer B: Radar Layer
- `monster_radar_daily.csv`
- 產出高爆發候選，標記 `300%/500%/1000%觀察`
- `radar_signals_daily.csv`
- Multi-radar（`sector_rotation` / `post_earnings_drift` / `squeeze_setup`）

### Layer C: Decision Support Layer
- `ai_focus_list.csv`
- `fusion_top_daily.csv`
- `ai_ready_bundle.xlsx`

### Layer C.5: Feature Layer
- `feature_signals_daily.csv`
- Feature score：`feature_alpha_score_v1`
- 核心欄位：momentum accel / squeeze pressure / float tightness proxy / earnings catalyst / news velocity proxy

### Layer C.8: Ranking + Decision Layer
- `ranking_signals_daily.csv`
- `decision_signals_daily.csv`
- Regime-aware 排序 + 決策標籤（`keep/watch/replace_candidate`）+ 失效條件
- Scanner profile：`balanced` / `monster_v1`

### Layer D: Evaluation Layer
- `xq_pick_log.csv`
- `ai_decision_log.csv`
- `weekly_report_latest.md`

### Layer E: AI Research Bridge
- `ai_research_candidates.csv`
- `ai_research_prompt.md`
- `ai_research_manifest.json`

### Layer E.5: Dual-Mode AI Analysis
- Web mode（free）：網頁 AI + `ai_research_prompt.md`
- API mode（fallback）：Tavily Search + Gemini Flash
- API 輸出：`api_catalyst_analysis_daily.csv` / `api_catalyst_brief.md`

---

## 4. Data Contract（v1）

## 4.1 `monster_radar_daily.csv`
必要欄位：

- `股票代碼`
- `妖股分數`
- `潛力等級`（`300%觀察` / `500%觀察` / `1000%觀察` / `高波動觀察`）
- `型態階段`（`啟動` / `擴散` / `回檔` / `過熱`）
- `明日偏向`（`中性` / `中性偏多` / `偏多` / `偏多(高波動)`）
- `今日漲幅%`
- `週漲幅%`
- `量能倍數`
- `市值`
- `股價`
- `理由摘要`

選配欄位：

- `預測上行%`
- `分析師數`
- `財報狀態`
- `距離財報天數`
- `產業`
- `評級`
- `vwap`, `sqz_on`, `sqzmom_color`, `sqzmom_value`, `signal_age`

---

## 4.2 AI 輸入整包（`ai_ready_bundle.xlsx`）
必備 sheet：

- `ai_focus_list`
- `fusion_top_daily`
- `monster_radar_daily`
- `raw_market_daily`
- `theme_heat_daily`
- `theme_leaders_daily`
- `xq_short_term_updated`

---

## 5. 核心策略說明（v1）

1. **Radar 先篩**：用動能 + 量能 + 市值 + 催化 + TV 訊號做 Monster Score。
2. **分級標註**：輸出 300/500/1000 觀察層級。
3. **AI 決策輔助**：把 Radar 候選放進 AI focus 優先序。
4. **週報驗證**：由每週報告驗證「Radar 是否提升命中率」。

補充：

- 第一層是 Scanner（找股票），核心是量能/市值/事件/題材，不是技術指標本身
- 第二層才是 timing（VWAP/SQZMOM）與進場風險控管

---

## 6. 風險與聲明

- `300%/500%/1000%` 僅代表候選強度，非報酬承諾。
- 高分標的可能同時伴隨高回撤。
- 本系統僅提供研究與決策輔助，不提供投資建議。

---

## 7. 接下來的實作順序（No Auto-Execution）

1. 強化 Monster Score（加入 float/short interest/news velocity）
2. 建立事件偵測引擎（財報驚喜、公告、族群共振）
3. 建立預測校準（Platt / Isotonic）
4. 增加分層權重校準（依 regime 與波動環境動態調參）
5. 增加週報維度（分級命中率、MFE/MAE、持有期敏感度）

---

## 8. 驗收標準（v1）

- 每日固定產生 `monster_radar_daily.csv`
- `ai_ready_bundle.xlsx` 包含 `monster_radar_daily` sheet
- AI Protocol 可直接讀取 Monster Radar
- 每週報告可追蹤 Radar 對勝率/報酬的增益

---

## 9. 實作進度（2026-03-05）

- ✅ 已完成 `ai_trading` 模組骨架（contracts / pipeline / event detector）
- ✅ 已完成 `scripts/build_ai_trading_dataset.py`
- ✅ 已接入 `run_daily.bat`（XQ 更新後自動產生 dataset + event signals）
- ✅ 已有 `repo_outputs/ai_trading/latest/*` 固定輸出
- ✅ 已完成特徵工程引擎（feature_alpha_score_v1 + feature_signals_daily）
- ✅ 已完成多雷達掃描器（Sector Rotation / Post-Earnings Drift / Squeeze）
- ✅ 已完成排名計分引擎（ranking_signals_daily + regime-aware 權重）
- ✅ 已完成 Decision/Risk Layer（decision_signals_daily + keep/watch + invalidation）
- ✅ 已完成 AI 研究橋接層（candidates + prompt）
- ✅ 已完成雙模式研究流（Web free / API fallback）
- ⏳ 下一步：實作權重校準與回測回饋（Platt/Isotonic + 週報指標擴充）
