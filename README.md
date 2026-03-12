# Alpha Finder — 無腦投資

> 4 個步驟 + Discord 命令，就搞定

## 你要做的

1. **每天跑產線**

```powershell
.\run_daily.bat
```

2. **把 Bundle 丟給 AI**

把這兩個檔案給網頁 AI 分析：
- `repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx`
- `Alpha-Sniper-Protocol.md`

3. **放回決策檔**

AI 產出 `ai_decision_YYYY-MM-DD.csv` 後，放進 `repo_outputs/backtest/inbox/`

4. **歸檔決策**

```powershell
python .\scripts\record_ai_decision.py --auto-latest
```

（若同一天想用新版本完全取代，改成：`python .\scripts\record_ai_decision.py --auto-latest --replace-date`）

完成。盤中提醒與成交追蹤全自動，看 Discord。

## Discord 怎麼用

所有操作都在 Discord：

```text
/positions        — 看你現在的部位
/buy              — 回報買進（可加 profile=monster 或 swing，預設 monster）
/add              — 回報加碼
/sell             — 回報賣出
/trades           — 看你所有成交紀錄
/executions       — 看系統執行歷史（engine + TradingView alerts）
```

**推薦流程：**

```text
/positions              — 一開盤先看目前部位
/buy AAPL qty amt       — 有成交就回報
/add AAPL qty amt
/sell AAPL qty amt
... 系統接著自動監控並推提醒給你 ...
```

系統會自動推給你：
- **風控警報**（⚡ 風控提醒）：止損/部分獲利 → **立即推送**
- **早晨摘要**：今天top幾個關注股 + 昨日持倉狀態 → **23:17 UTC 推送**
- **開盤監控**：開盤前後風險檢查 + 早晨 Top1 關注提醒 → **14:38/46 UTC 及 01:38/46 UTC 推送**

## 一周回看什麼

周末快速複盤，看這些檔案：

- `repo_outputs/backtest/ai_decision_log.csv` — 你所有決策紀錄
- `repo_outputs/backtest/execution_trade_log.csv` — 系統成交與訊號歷史
- `repo_outputs/backtest/weekly_reports/weekly_report_latest.md` — 周報告（win rate, PnL）

## 碎碎念

- 系統不會自動下單，只會計算訊號並推提醒
- 你在 Discord 回報成交，系統才更新持倉
- 盤中訊號分兩層：風控出場立即推 Discord；進場訊號寫記錄由早晨摘要整合
