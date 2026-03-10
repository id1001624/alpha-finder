# cloud_state

這個資料夾只放給 GitHub Actions 讀取的最小 runtime 狀態。

目前同步目標：

- ai_decision_latest.csv
- positions_latest.csv
- execution_trade_latest.csv（有資料時才會同步）

設計原則：

- 本機 canonical 檔案仍然寫在 repo_outputs/backtest/
- cloud_state 只是同步副本，不是主要寫入位置
- GitHub Actions 優先讀 cloud_state，避免直接依賴被 ignore 的 repo_outputs/
- Discord bot 回報成交後不會自動 commit/push；若未來要真正即時雲端同步，應改成外部存放