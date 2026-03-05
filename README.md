# Alpha Finder 個人操作手冊

## 這個專案在做什麼

這個專案是我的「AI Trading 情報引擎」，用途是：

- 每天掃描市場，先找出可能爆發的股票
- 用多層分數做排序（feature/radar/event/ranking/decision）
- 輸出給我和網頁 AI 做研究判斷
- 不做自動下單

核心定位：

- 第一層：找股票（Scanner / Research / Ranking）
- 第二層：交易時機（VWAP / SQZMOM）

## 我每天要做什麼

1. 跑日更

```powershell
.\run_daily.bat
```

2. 跑雙策略比較（建議每天都跑）

```powershell
.\run_ai_compare.bat web
```

3. 讀當日重點輸出

- `repo_outputs/ai_trading/latest/decision_signals_daily.csv`
- `repo_outputs/ai_trading/latest/ai_research_candidates.csv`
- `repo_outputs/ai_trading/profile_compare/latest/profile_compare_summary.md`

4. 把研究檔給 AI（網頁版）

- `ai_research_candidates.csv`
- `ai_research_prompt.md`

## Web / API 開關

`AI_RESEARCH_MODE` 只需要填模式字串：

- `web`：免費，預設模式
- `api`：網頁額度用完時切換，走 Tavily + Gemini API

即時切換（只影響目前終端）：

```powershell
$env:AI_RESEARCH_MODE='web'   # 或 api
$env:CATALYST_DETECTOR_ENABLED='true'
```

永久切換（寫入使用者環境變數）：

```powershell
[System.Environment]::SetEnvironmentVariable('AI_RESEARCH_MODE','api','User')
[System.Environment]::SetEnvironmentVariable('CATALYST_DETECTOR_ENABLED','true','User')
```

## 兩個流程分別在幹嘛

Web 流程（免費）：

- 系統先輸出研究候選與提示詞
- 我把檔案餵給網頁 AI
- 快速低成本

API 流程（備援）：

- 系統直接呼叫 Tavily 搜尋新聞
- 再呼叫 Gemini 判斷催化與爆發機率
- 產出：
  - `api_catalyst_analysis_daily.csv`
  - `api_catalyst_brief.md`

## Scanner 比較（balanced vs monster_v1）

目前先雙跑，不先硬合併：

- `balanced`：覆蓋面廣，穩定日常用
- `monster_v1`：條件更嚴，專抓妖股

比較報告看：

- `repo_outputs/ai_trading/profile_compare/latest/profile_compare_summary.md`

## 每週回測資料多久丟給你一次

建議頻率：每 7 天一次（固定週日或每 5 個交易日）。

每週丟這幾個給你檢查就夠：

- `repo_outputs/backtest/weekly_reports/weekly_report_latest.md`
- `repo_outputs/backtest/weekly_reports/weekly_trades_latest.csv`
- `repo_outputs/backtest/weekly_reports/weekly_ai_trades_latest.csv`
- `repo_outputs/ai_trading/profile_compare/latest/profile_compare_summary.csv`

如果遇到連續 2-3 天績效異常，提早丟，不用等一週。

## Auto-hybrid v2 提醒

下週要做：Auto-hybrid v2

目標：

- 用最近 2-4 週的比較結果自動調整權重
- 不再手動判斷當天用 balanced 或 monster_v1
- 輸出單一主策略清單 + 保留備援清單

## 兩個 bat 還要不要留

`setup_schtasks.bat`：

- 只在「第一次建立排程」時需要
- 如果排程已建立成功，可以留著備用，也可以刪

`run_ai_compare.bat`：

- 建議保留
- 這是每天比較 balanced/monster_v1 的一鍵入口

## 快速自檢

確認 API 環境是否生效：

```powershell
c:/Users/w6359/OneDrive/文件/alpha-finder/.venv/Scripts/python.exe -c "import config; print(config.AI_RESEARCH_MODE, config.CATALYST_DETECTOR_ENABLED, bool(config.TAVILY_API_KEY), bool(config.GEMINI_API_KEY), config.GEMINI_MODEL)"
```
