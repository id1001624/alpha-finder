# Alpha Finder 每日 SOP

這份文件是你每天真正要看的操作版。現在專案的定位是：把真實資料整理好，交給網頁版 AI 做搜尋與分析，你再用 Discord / 決策檔做最後判斷。

## 你每天只要看這 3 件事

1. 先跑晨間掃描，產生候選與 prompt。
2. 把 prompt / 候選資料貼到網頁版 Grok 或 Perplexity，拿回結果 JSON。
3. 跑決策寫入與歸檔，最後只看 Discord 與 ai_decision。

## 每日流程

### 1. 06:00 晨間掃描

```powershell
python .\scripts\run_morning_scan.py
```

這一步會：
- 讀 regime
- 掃 Finviz + XQ
- 產出 `output/YYYY-MM-DD/candidates_scored.csv`
- 產出網頁板要用的 prompt 檔

你要看的重點檔案：
- `output/YYYY-MM-DD/perplexity_prompt.txt`
- `output/YYYY-MM-DD/grok_narrative_prompt.txt`
- `output/YYYY-MM-DD/candidates_scored.csv`

### 2. 用網頁版 AI 做分析

把晨間產生的 prompt 與候選資料，貼到網頁版 Grok / Perplexity，請它回 JSON。

你要回填的檔案：
- `output/YYYY-MM-DD/perplexity_result_manual.json`
- `output/YYYY-MM-DD/grok_narrative_manual.json`
- `output/YYYY-MM-DD/grok_sentiment_result_manual.json`

### 3. 盤前情緒整理

```powershell
python .\scripts\run_grok_sentiment.py --date YYYY-MM-DD
```

如果你已經手動放好 `grok_sentiment_result_manual.json`，這一步就會讀手動結果並輸出：
- `output/YYYY-MM-DD/candidates_filtered.csv`

### 4. 產生決策與歸檔

```powershell
python .\scripts\run_ai_decision_v31.py --date YYYY-MM-DD
python .\scripts\record_ai_decision.py --auto-latest --include-preview-sources
```

這一步會輸出：
- `output/YYYY-MM-DD/ai_decision.csv`
- `repo_outputs/backtest/ai_decision_latest.csv`
- Turso latest state

### 5. 你最後只要看 Discord

重點不是手動盯 API，而是看：
- AI 決策結論
- Discord 成交回報
- positions / execution logs

## 你要丟給網頁版 Grok 的重點檔案

優先順序如下：

1. `output/YYYY-MM-DD/candidates_scored.csv`
2. `output/YYYY-MM-DD/perplexity_prompt.txt`
3. `output/YYYY-MM-DD/grok_narrative_prompt.txt`
4. `output/YYYY-MM-DD/grok_sentiment_prompt.txt`
5. `repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx`
6. `repo_outputs/ai_ready/latest/Alpha-Sniper-Protocol.md`
7. `docs/V3.1_SPEC.md`

## 這專案現在的重點腳本

- `scripts/run_morning_scan.py`：晨間掃描與 prompt 產生
- `scripts/run_grok_sentiment.py`：盤前情緒整理與手動回填讀取
- `scripts/run_ai_decision_v31.py`：v3.1 決策組裝與歸檔
- `scripts/record_ai_decision.py`：既有決策歸檔與 Turso 同步
- `scripts/regime_detector.py`：市場三態判定
- `scripts/finviz_momentum_scanner.py`：Finviz 動能雷達
- `scripts/baseline_tracker.py`：XQ baseline 補充
- `backtest/backtest_crisis_periods.py`：股災回測
- `backtest/tune_params.py`：參數調優
- `backtest/visualize_backtest.py`：回測視覺化

## 一句話版本

你每天做的事其實很簡單：

1. 跑晨掃。
2. 把 prompt 丟給網頁版 AI。
3. 回填 JSON。
4. 跑決策與歸檔。
5. 看 Discord。