# Runbook — ledger replay / consistency check

Use after ledger-db incidents or when Harbor Subscriptions reports balance
drift.

## Steps

1. Put ledger-service in read-only mode.
2. Replay the last N minutes of journal entries from the append-only log.
3. Diff balances for Harbor Subscriptions and Brightline Retail sample
   accounts.
4. Clear read-only mode only after Sam Ortiz (ledger-db owner) signs off.

## Related

- INC-1042 used this after pool saturation recovered.
