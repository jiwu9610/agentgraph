# Customers

- **Brightline Retail** — high volume. Uses `checkout-web` and relies on
  `payments-api` capture. SLA: 99.9% successful capture.
- **Harbor Subscriptions** — recurring billing. Sensitive to ledger lag;
  uses `ledger-service` reporting exports.
- **Northwind Labs** — low volume pilot. Uses `checkout-web` only.

When an incident impacts `payments-api` capture, page Brightline's account
channel first.
