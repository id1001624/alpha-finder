# Alpha Sniper v3 工程師移交文件

版本：v3.0
撰寫人：Nyver（產品決策）+ AI（架構設計）
日期：2026-03-20
閱讀對象：負責 Alpha Sniper 專案的工程師

---

## 一、為什麼要改：舊版根本問題

舊版 Alpha Sniper 最大問題是「本地先決定候選宇宙」。
具體來說：

1. bundle 跑完 31 張 sheet 之後，decision_signals_daily 和
   ranking_signals_daily 就決定了哪些票「值得看」。
2. Perplexity AI 只能在這個已經被本地篩過的 Top 10 裡面修修補補。
3. 結果是：真正在市場噴出的主升股（AXTI、LITE、JTAI 等）
   如果一開始沒進 decision_signals_daily，AI 永遠看不到它。
4. 31 張 sheet 大量計算，但最終輸出的 ai_decision 裡
   連 XQ 當天強勢榜前幾名都沒有。

結論：不是資料不夠，是架構的目標函數錯了。
以前是「預測哪些會漲」，現在要改成
「找出市場已經在動的主題和票，用本地資料確認階段和風控，
再讓 AI 壓縮成 Top 1 / Top 5」。

---

## 二、v3 架構：三層分工

v3 把整個系統分成三層，每層職責不重疊：

### Layer 1：市場雷達層（Web + 外部資料，由 AI 主導）

- 職責：生成候選宇宙（每天 50~200 檔）
- 執行方式：Perplexity AI 每天盤前搜尋，不是程式自動跑
- 來源白名單：
  - XQ 每日強勢股清單（你本地已有）
  - StockTitan rankings / news / SEC live feed（免費）
  - StockAnalysis earnings calendar（免費）
  - Benzinga earnings calendar（免費/Pro）
  - Finviz 動能篩選（你專案已有 API）
  - 官方 SEC EDGAR / 公司 IR（免費）
- 輸出：候選 ticker 清單 + 初步 theme label + catalyst flag
- 重要：Layer 1 的輸出不寫進 bundle。
  這是 Nyver 和 AI 的對話產出，用來補充 Local Pool。

### Layer 2：Local 判讀層（精簡後的 bundle，由工程師負責）

- 職責：對傳入的 ticker 清單做技術 gate + 趨勢階段判斷 + 風控排雷
- 輸入：可以是 Local 自己跑的候選，也可以接受 Layer 1 傳進來的外部 ticker
- 輸出：每檔標記 trend_stage + playbook_type + trade_eligibility
- 重要：Layer 2 不決定「要不要買」，只回答三個問題：
  1. 這檔現在在趨勢哪個階段？（early / mid / late）
  2. 有沒有技術面問題？（VWAP / SQZMOM / volume gate）
  3. 有沒有風控地雷？（稀釋 / ATM / offering / reverse split）

### Layer 3：決策輸出層（AI 最終壓縮）

- 職責：合併 Layer 1 + Layer 2，輸出唯一 Final CSV
- 執行方式：Nyver 把 Layer 1 篩完的清單 + Layer 2 的判讀結果給 AI
- AI 用 TriggerScore + 白名單規則決定 Top 1 / Top 5
- 輸出：ai_decision_YYYY-MM-DD.csv（32 欄，見第五節）

---

## 三、bundle 要怎麼改（重點）

### 3.1 bundle 的新定位

舊版 bundle 做的事太多也太雜：
- 既要決定候選宇宙
- 又要算分排序
- 又要管事件、財報、主題、指標
- 31 張 sheet，大部分是為了讓本地算分更準確

v3 之後 bundle 的新定位只有一件事：
「把傳進來的 ticker，快速判斷它的趨勢階段、技術狀態、風控問題，
然後輸出標準欄位讓 AI 用。」

bundle 不再是候選生成器，是候選判讀器。

### 3.2 Sheet 精簡：從 31 張砍成分層

請工程師把現有 31 張 sheet 分成三個 tier：

Tier-1（主排序必用，保留）：
- decision_signals_daily：技術面 + gate
- ranking_signals_daily：local rank / scores
- market_dataset_daily：基本面、主題、雷達信號
- monster_radar_daily：主升候選庫
- ai_focus_list：重點關注池

Tier-2（風控輔助，保留但不進主排序）：
- pre_event_watchlist：財報/事件預熱
- bundle_contract_status：bundle 狀態確認
- xq_short_term_updated：XQ 短線更新
- ai_research_candidates：研究候選輔助

Tier-3（直接移除主排序 pipeline，降級成研究存檔）：
- 其餘所有 sheet（預計約 17-22 張）
- 這些 sheet 不得再影響 decision_signals_daily 的候選選擇
- 如果後續要做量化模型（v4 以後），才再考慮拿回來用

請工程師確認哪些 sheet 目前在 pipeline 裡影響主排序，
把 Tier-3 的全部從主 pipeline 移除。
bundle 生成時間應該可以縮短 30~60%。

### 3.3 新增兩個必要欄位（工程師要加進 bundle pipeline）

工程師需要在 decision_signals_daily（或 ranking_signals_daily）新增：

欄位一：trend_stage
- 用途：告訴 AI 這檔在趨勢哪個階段
- 允許值：early_trend / mid_trend / late_parabolic
- 判斷規則（請工程師按此實作）：
  - early_trend：
    daily_change_pct > 5%
    AND momentum_accel_1d_3d > 0（若無此欄，改用 daily_change_pct > prev_day）
    AND open_exhaustion_risk_score < 40
    AND volume_gate_status != blocked
  - mid_trend：
    open_exhaustion_risk_score between 20 and 59
    AND vwap_status == aligned
    AND sqzmom_status != pending_confirmation 以上的負值
    AND decision_tag_v1 == keep
  - late_parabolic：
    open_exhaustion_risk_score >= 60
    OR tomorrow_entry_readiness == avoid_chase
    OR volume_gate_status starts with blocked
    OR protocol_gate_reason 含 exhaustion_hard_block / premarket_too_hot

欄位二：playbook_type
- 用途：告訴 AI 這檔適合什麼打法
- 允許值：core_trend / swing_trend / speculative_pump
- 判斷規則（請工程師按此實作）：
  - core_trend：
    market_cap > 2,000,000,000（2B USD）
    AND revenue_growth_yoy > 0（正成長）
    AND risk_level NOT IN [high]
    AND invalidation_rule 無 offering/atm/dilution
  - swing_trend：
    market_cap between 200,000,000 and 2,000,000,000
    OR（有主題 tag AND 財報在 T+1 到 T+30 內）
    AND invalidation_rule 無 dilution 類
  - speculative_pump：
    market_cap < 200,000,000
    OR invalidation_rule 含 offering/atm/warrant/convertible/reverse_split
    OR 歷史稀釋紀錄（若有此欄位）

如果現有 bundle 缺少 market_cap 欄位，
請從 market_dataset_daily 或 yfinance 補進來。

### 3.4 Finviz API 的使用方式

你專案現有 Finviz API 寫在 bundle 裡。
v3 建議把 Finviz 的動能篩選從 bundle 主流程拆出來，
獨立成一個單獨排程腳本，理由是：

- bundle 的用途是「判讀傳進來的 ticker」，不是「產生全市場掃描結果」
- Finviz 動能篩選是 Layer 1 市場雷達層的工作
- 把它混在 bundle 裡會讓 bundle 又回到「本地決定宇宙」的老問題

建議拆成獨立腳本：finviz_momentum_scanner.py
每天開盤前跑一次，用以下條件：
- Country = USA
- Volume > 500,000
- Performance Week > 5%
- Price > 2
輸出：momentum_pool_YYYY-MM-DD.csv

這份 CSV 不寫進 bundle，而是直接傳給 AI（Nyver 貼給我）用來補充候選池。

### 3.5 Finviz 和 bundle 的銜接

Nyver 每天的流程是：
1. 跑 finviz_momentum_scanner.py → 得到 momentum_pool CSV
2. AI 看 momentum_pool + 主題搜尋結果 → 篩成 30 檔候選清單
3. 這 30 檔 ticker 丟進 bundle → 跑 Local 判讀（trend_stage + playbook_type + gate）
4. bundle 輸出判讀結果 → AI 合併 Layer 1 + Layer 2 → 輸出 ai_decision CSV

bundle 這樣做不會破壞 Layer 1 篩選結果，因為：
- bundle 只輸出「這檔的技術狀態和風控」
- 它不再決定要不要選這檔進最終名單
- 最終壓縮 Top 1 / Top 5 由 AI 在 Layer 3 做

---

## 四、每日執行 SOP（給 Nyver 的操作流程）

這一節不是工程師要做的事，是 Nyver 的每日操作。
工程師只需要確保：
(a) finviz_momentum_scanner.py 可以排程跑
(b) bundle 可以接受外部 ticker 清單輸入並只跑 Layer 2 判讀

Nyver 每天盤前 30~40 分鐘流程：

Step 1（5分鐘）：讓 AI 做題材掃描
問 AI：「今天美股最強的 3-5 個題材是什麼，
每個題材舉代表股和次強股，
有沒有今明兩天有財報或 8-K 硬催化的？」

Step 2（5分鐘）：跑 Finviz 動能掃描
執行 finviz_momentum_scanner.py
得到 momentum_pool CSV（預計 20~50 檔）

Step 3（5分鐘）：看 XQ + StockTitan
從 XQ 當日強勢清單挑前 5~10 檔
從 StockTitan rankings 補 1~2 檔
加進候選池

Step 4（5分鐘）：合併候選池，傳給 AI
把 Step 1+2+3 的候選清單（最多 30~40 檔）傳給 AI
AI 用 TriggerScore + 白名單規則做第一輪篩選
刪掉有稀釋風險或 TriggerScore < 7 的
剩下 10~15 檔

Step 5（5分鐘）：跑 bundle Layer 2
把篩完的 10~15 檔 ticker 丟進 bundle
bundle 輸出 trend_stage + playbook_type + gate 狀態

Step 6（5分鐘）：AI 最終壓縮輸出
把 bundle 輸出結果給 AI
AI 輸出 ai_decision_YYYY-MM-DD.csv（Top 1 / Top 5）

Step 7（收盤後，5分鐘）：記錄回測基準
記錄 Top 1 的收盤報酬
記錄 XQ 當日強勢前 5 的平均報酬（baseline）
這是唯一判斷這套流程是否進步的方法

---

## 五、ai_decision CSV 欄位契約（32 欄，v3 版）

欄位順序固定，工程師請確保 pipeline 輸出以下順序：

1.  decision_date
2.  rank
3.  ticker
4.  short_score_final
5.  swing_score
6.  core_score
7.  risk_level
8.  tech_status
9.  theme
10. decision_tag
11. reason_summary（必含：進場語意 + 催化原因 + 失效條件）
12. source_ref
13. research_mode（bundle_only / bundle_plus_web）
14. catalyst_type
15. catalyst_sentiment
16. explosion_probability
17. hype_score
18. confidence
19. api_final_score
20. catalyst_source
21. catalyst_summary
22. local_rank
23. local_decision_tag（原始 keep/watch/replace，不得改寫）
24. trade_eligibility（tradable / downgraded / blocked / watch_only）
25. candidate_origin（local / web_challenger）
26. web_override_flag（true / false）
27. web_override_reason（不可模糊，必須是具體事件）
28. web_delta_score（整數，-100 到 +100，0 = 無影響）
29. trend_stage（early_trend / mid_trend / late_parabolic）
30. playbook_type（core_trend / swing_trend / speculative_pump）
31. execution_action（BUY_SCALE_IN / BUY_AGGRESSIVE / SELL_SCALE_OUT / SELL_ALL_EXIT / NO_TRADE）
32. exit_action

相關規則：
- trade_eligibility != tradable 時，execution_action 不得為任何 BUY_*
- web_override_flag = true 時，web_override_reason 必須有值
- candidate_origin = web_challenger 時，source_ref 必須列 Web 來源 URL

---

## 六、v2 vs v3 差異對照

| 項目 | v2（舊版） | v3（新版） |
|---|---|---|
| 候選宇宙由誰決定 | Local bundle 決定 | 市場 + Web + Local 三池合併 |
| Web 的角色 | 只能在 Local Top 10 內確認 | 可生成 Challenger，每日最多 2 檔進 Top 5 |
| bundle 職責 | 決定候選 + 評分排序 | 只做趨勢判讀 + 技術 gate + 風控排雷 |
| sheet 數量 | 31 張全進主排序 | Tier-1 五張主排序，其餘降級 |
| Finviz API | 混在 bundle 裡 | 獨立腳本，輸出 momentum_pool CSV |
| CSV 欄位數 | 21 欄 | 32 欄 |
| trend_stage | 無 | 新增，early / mid / late |
| playbook_type | 無 | 新增，core / swing / speculative |
| 回測基準 | 無 | 每日記錄 vs XQ baseline |

---

## 七、v4 以後再做的事（現在先不動）

以下功能請工程師知道我們有計劃，但 v3 完成穩定後才執行：

1. LLM 讀財報/新聞做看多/看空訊號
   - 用 Perplexity / OpenAI API 批次讀財報逐字稿 + 新聞
   - 輸出 sentiment_signal（bullish / neutral / bearish）+ 信心分數
   - 來源：SEC 10-K / 10-Q / 8-K / 財報會議逐字稿

2. 多因子模型 + 時間序列回歸
   - 用 yfinance + scikit-learn 做多因子打分
   - 因子：動能、成長、估值、財務穩定、主題曝險
   - 目標：把 9000 檔市場排出前 1% 候選，補充 Layer 1

3. LSTM / Random Forest / Gradient Boosting
   - 用 yfinance + TensorFlow/Keras 或 scikit-learn
   - 訓練目標：預測 5 日方向（漲/跌/橫）
   - 輸入特徵：歷史價量 + 基本面 + 技術指標
   - 不作為獨立決策依據，只作 AI 的輔助特徵分數

4. GNN + 供應鏈關聯圖
   - 把供應鏈、產業鏈、客戶關係建成圖
   - 預測哪些公司會因上游/下游事件受益或受損
   - 技術複雜度最高，排最後做

以上四項現在全部不做，placeholder 先留在架構圖裡，
等 v3 的回測 baseline 穩定打贏 XQ 強勢榜後再開啟。

---

## 八、工程師的具體待辦清單

請工程師依優先順序完成以下事項：

優先級 1（v3 核心，必須先做）：
- [ ] 確認現有 31 張 sheet 哪些在主排序 pipeline 裡
- [ ] 把 Tier-3 sheet 從主 pipeline 移除
- [ ] 把 Finviz API 從 bundle 拆成獨立腳本 finviz_momentum_scanner.py
- [ ] 在 decision_signals_daily 新增 trend_stage 欄位（見第三節判斷規則）
- [ ] 在 decision_signals_daily 或 ranking_signals_daily 新增 playbook_type 欄位（見第三節）
- [ ] 確認 market_cap 欄位存在（playbook_type 需要用到）
- [ ] CSV 輸出升級為 32 欄（見第五節）
- [ ] 確認 bundle 可以接受外部 ticker 清單輸入，只跑 Layer 2 判讀

優先級 2（v3 完整性）：
- [ ] 建立 baseline_tracker.py：每日記錄 Top 1 報酬 vs XQ baseline
- [ ] 把舊版 Protocol 更新為 v3 版本（以本文件為準）
- [ ] 確認 FINAL_DECISION_COLUMNS 對齊 32 欄（record_ai_decision.py）
- [ ] trade_eligibility enum 確認只允許：tradable / downgraded / blocked / watch_only
- [ ] execution_action enum 確認移除 BOT_ONLY

優先級 3（v4 準備，現在先不動）：
- [ ] LLM 財報/新聞情緒訊號
- [ ] 多因子模型
- [ ] LSTM / ML 預測層
- [ ] GNN 供應鏈圖

---

## 九、一句話給工程師的核心原則

bundle 從今天起的職責是：
「我不決定誰值得看，我只告訴你值得看的這些票，現在在哪個階段、
有沒有技術問題、有沒有地雷。」
