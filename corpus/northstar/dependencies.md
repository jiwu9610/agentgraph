# Runtime dependencies

Edges below are production runtime dependencies (not repo import graphs).

- `checkout-web` **depends on** `payments-api`.
- `payments-api` **depends on** `ledger-service`.
- `payments-api` **depends on** `risk-engine`.
- `payments-api` **depends on** `payments-cache`.
- `ledger-service` **depends on** `ledger-db`.
- `risk-engine` **depends on** `risk-redis`.
- `notify-worker` **depends on** `payments-api` (reads capture events).

If `ledger-db` is degraded, `ledger-service` fails, then `payments-api`
capture paths fail, then `checkout-web` shows payment errors.
