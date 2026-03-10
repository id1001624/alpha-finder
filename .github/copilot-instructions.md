# Alpha Finder - Copilot Instructions

## Project Overview

Alpha Finder 現在是一條完整的 AI 研究、決策、盤中執行提醒、持倉追蹤工作流。

主流程：

```text
run_daily.bat
  -> repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx
  -> Web 或 API 產出 ai_decision_YYYY-MM-DD.csv
  -> scripts/record_ai_decision.py 歸檔
  -> ai_decision_latest.csv 成為盤中 engine watchlist
  -> intraday engine 計算分鐘級訊號
  -> Discord Bot 接收真實成交回報
  -> positions / execution logs 持續更新
```

專案核心目標：

- 找出明日最可能延續上漲的 Top 1 與 Top 5
- 維持 ai_decision_YYYY-MM-DD.csv 決策契約穩定
- 讓使用者只做最少人工步驟，盤中主要透過 Discord 操作
- 不自動下單，只做研究、提醒、成交回報與追蹤

---

## Operating Model

### 1. Web Mode Is Still Default

- AI_RESEARCH_MODE='web' 是預設
- 日常提供給網頁 AI 的固定組合：
  - repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx
  - Alpha-Sniper-Protocol.md
- 網頁 AI 最終必須輸出 ai_decision_YYYY-MM-DD.csv
- 使用者再執行 python .\scripts\record_ai_decision.py --auto-latest

### 2. API Mode Is Fallback

- AI_RESEARCH_MODE='api' 時，系統走 Tavily + Gemini 備援
- 只有真的拿到有效決策列時，才可寫出 ai_decision_YYYY-MM-DD.csv
- 不允許用純本地排序偽裝成 AI 決策

### 3. Intraday Operation Is Repo-Native

- 盤中主流程已經不是依賴手動 TradingView alert 維護
- ai_trading/intraday_execution_engine.py 自己抓分鐘資料並計算 Dynamic AVWAP + SQZMOM
- scripts/run_discord_trade_bot.py 是真實成交回報入口
- engine / recap 正式排程已經以 GitHub Actions 為主，不應再把本機夜間 Windows 排程描述成主流程
- watchlist、持倉、成交與 execution history 現在優先讀 Turso，最後才 fallback 到本機 CSV

### 4. Discord Is The User Surface

- 使用者平常在 Discord 用 /buy、/add、/sell、/positions、/trades、/executions
- slash commands 是主介面
- prefix commands 只是相容保留，不是主操作方式

---

## Architecture Notes

### 1. Bundle-First Input

- repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx 是 web AI 的統一入口
- 不要再回到舊的多檔分散輸入模式，除非使用者明確要求

### 2. Decision Contract Is Stable

ai_decision_YYYY-MM-DD.csv 是穩定契約，核心欄位包括：

- 基礎決策欄位：decision_date, rank, ticker, short_score_final, swing_score, core_score, risk_level, tech_status, theme, decision_tag, reason_summary, source_ref
- 催化欄位：research_mode, catalyst_type, catalyst_sentiment, explosion_probability, hype_score, confidence, api_final_score, catalyst_source, catalyst_summary

修改流程時優先維持這份契約穩定。

### 3. Intraday Data Provider

- INTRADAY_DATA_PROVIDER=auto|finnhub|yfinance
- auto 會優先用 Finnhub 免費分鐘資料
- 拿不到或不相容時 fallback 到 yfinance

### 4. Cloud-First Runtime

- `intraday monitor`、`bedtime recap`、`morning recap` 已經是雲端正式主路徑
- 本機 Windows 排程現在只應描述為備援，不是預設夜間運作模型
- `setup.bat` 應預設停用本機 recap 與本機 intraday engine 排程
- `cloud_state/` 已退場，不應再被當成正式 runtime 路徑描述
- Discord Bot 仍可能用本機登入自啟或 Startup fallback，直到未來搬去雲端 host

---

## Project Conventions

### 1. Config First

- 所有門檻、模式切換、資料源選擇優先看 config.py
- 不要把業務數字硬寫進流程檔

### 2. Preserve The Short Operator Flow

- README 應該維持最短操作路徑
- 不要把已經自動化的東西重新寫成手動步驟給使用者執行
- 睡前摘要與早晨 recap 應描述為雲端既有能力，不要再寫成使用者夜間要靠本機排程維持
- 若 README 提到 Windows 排程，應明確標示為備援，而不是正式主流程
- 若 Turso 已是正式狀態源，README 不應再把 `cloud_state/` 描述為必要備援層

### 3. No Fake AI Outputs

- 不要把本地排序結果假裝成 AI 決策 CSV
- API 決策失敗時應回傳 disabled / no rows，而不是寫出假決策檔

### 4. Minimal Scope

- 修改時優先最小變更
- 不要順手改 unrelated strategy / backtest 邏輯

### 5. Git Rules

- commit 與 push 規則除了本檔外，還必須同步遵守 `.github/prompts/GitRules.prompt.md`
- commit message 預設採用帶 type 前綴的格式，例如：`feat(project): 新增 Turso 同步`
- 使用者說告一段落就自動做commit+push動作
- 若使用者明確要求 commit / push，先完成必要驗證，再依功能分組提交
- commit message 主旨預設使用繁體中文，內容要能看出變更主題
- 若同一次工作包含多個明顯獨立主題，優先做多個小 commit、最後一次 push
- 若使用者沒有明確要求，不要自動 commit 或 push

---

## Key Files

- README.md: 使用者操作手冊，應以簡單明瞭為優先
- config.py: 中央設定
- run_daily.bat: 日更主入口
- setup.bat: Windows 排程與自啟配置
- scripts/record_ai_decision.py: 決策歸檔
- scripts/run_intraday_execution_engine.py: 盤中 engine 啟動器
- scripts/run_discord_trade_bot.py: Discord 成交回報 bot
- ai_trading/intraday_execution_engine.py: 盤中訊號核心
- ai_trading/position_state.py: 持倉與成交 ledger
- turso_state.py: Turso 雲端 latest state / ledger / execution history 同步與查詢
- Alpha-Sniper-Protocol.md: 提供給網頁 AI 的決策 prompt

---

## Validation

修改後優先驗證：

1. Python 檔是否有語法或 Pylance 錯誤
2. run_daily.bat 是否仍能更新 repo_outputs/ai_ready/latest/ 與 repo_outputs/ai_trading/latest/
3. python .\scripts\run_intraday_execution_engine.py --dry-run --top-n 3 是否可執行
4. Discord Bot 環境變數是否可被 config.py 正確讀到
5. 若有更新 README，內容是否仍符合最短操作路徑

---

## Current Direction

- 目前基礎工作流已完成
- 後續若再做，應以優化與強化為主，不是回頭補基本能力
- 不要把專案方向拉回舊版 scanner-only 或 GSheets-only
