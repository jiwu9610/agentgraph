# Services and datastores

## Services

- **payments-api** — HTTP API that authorizes and captures charges. Talks to
  the ledger and the risk engine before capturing.
- **checkout-web** — Merchant-facing checkout UI. All charge creation goes
  through payments-api; checkout-web never talks to the ledger directly.
- **ledger-service** — Double-entry ledger. Source of truth for balances.
- **risk-engine** — Fraud scoring. Can soft-decline a charge before capture.
- **notify-worker** — Async email/SMS for receipts and decline notices.

## Datastores

- **ledger-db** — Postgres. Primary store for ledger-service.
- **risk-redis** — Redis. Feature flags and short-lived risk scores for
  risk-engine.
- **payments-cache** — Redis. Idempotency keys for payments-api.
