# Canonical Action/Event Log Architecture

## 目標

系統採用「雙引擎、單一事實來源」：

- Monster engine 與 Swing engine 可以不同策略與不同通知時機
- 所有對使用者有意義的 action/event 都先寫入同一份 canonical log
- Recap 與補發只讀 canonical log，不再各自維護平行訊息宇宙

## Canonical Log 檔案

- 檔案位置: `repo_outputs/backtest/canonical/canonical_action_event_log.csv`
- 實作模組: `ai_trading/canonical_event_log.py`

## 欄位契約

最低欄位契約如下：

- `event_ts`
- `trade_date`
- `engine` (`monster` / `swing`)
- `ticker`
- `action_type` (`entry` / `add` / `reduce` / `take_profit` / `stop_loss` / `exit`)
- `strategy_tag`
- `reason_code`
- `reason_text`
- `price_ref`
- `size_ref`
- `risk_unit`
- `priority`
- `dispatch_mode` (`realtime` / `recap`)
- `dispatch_status`
- `source_event_id`
- `source_log_id`
- `position_state_before`
- `position_state_after`
- `invalidation_rule`
- `created_at`

## 事件流

### Monster

1. Engine 產生 action（entry/add/take_profit/stop_loss）
2. 先寫入 canonical log (`dispatch_mode=realtime`, `dispatch_status=pending_realtime`)
3. 即時推播 Discord
4. 回寫 canonical `dispatch_status` 為 `sent_realtime` 或 `failed_realtime`

### Swing

1. Engine 產生 action（entry/add/reduce/exit）
2. 先寫入 canonical log
   - reduce/exit: `dispatch_mode=realtime`, `dispatch_status=pending_realtime`
   - entry/add: `dispatch_mode=recap`, `dispatch_status=pending_recap`
3. reduce/exit 即時推播 Discord，回寫 status
4. entry/add 由 recap/補發腳本接手，回寫為 `sent_recap` 或 `failed_recap`

### Recap

- Recap 讀取來源僅 canonical log
- 依 `event_ts`、`ticker`、`engine`、`action_type` 統一彙總
- 不再依賴 swing 專用 dispatch 檔或各自 message history

## 正式驗收（最近 5 交易日）

使用下列腳本做 artifact 驗收：

```bash
python scripts/verify_canonical_chain_last5.py
```

驗收重點：

- 最近 5 個交易日是否都有真實 canonical artifact
- 每日是否同時包含 monster/swing 事件
- action_type 是否在契約白名單
- dispatch_status 是否與 dispatch_mode 相容
- source_event_id/source_log_id/reason_text 是否可追溯
