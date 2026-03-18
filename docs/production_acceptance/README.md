# Intraday Production Acceptance

This folder stores fixed evidence for intraday production behavior.

## Fixed Artifact

- `docs/production_acceptance/intraday_prod_acceptance_2026-03-18.json`
  - Non dry-run real Discord send evidence.
  - Three message IDs (wait, buy, exit), timestamps, content, and jump links.
  - Same-minute dedupe proof.
  - Source trace to JTAI materialized contract row.

## How To Re-run Production Acceptance

1. Ensure the following are valid in runtime env:
   - `DISCORD_WEBHOOK_URL`
   - `repo_outputs/ai_trading/latest/ai_decision_contract_v2_materialized.csv` contains `JTAI` with `keep`, `final_priority=2`, `pullback_entry`.
2. Run:

```bash
python scripts/run_intraday_production_acceptance.py --output repo_outputs/backtest/alerts/intraday_prod_acceptance_latest.json
```

3. Verify output JSON contains:
   - `non_dry_run: true`
   - `wait_message.message_id`, `buy_message.message_id`, `exit_message.message_id`
   - `dedupe_same_minute.no_duplicate_sent: true`

## Workflow Failure Alert

- `.github/workflows/intraday-monitor.yml` now sends an extra Discord error alert when the workflow job fails.
- `scripts/run_intraday_execution_engine.py` now returns non-zero when any real Discord push fails (`wait`, `entry`, or `exit` channel).
