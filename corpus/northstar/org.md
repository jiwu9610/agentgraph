# Northstar Cloud — Organization

Northstar Cloud is a B2B payments platform. Engineering is split into two
teams.

## Teams

- **Payments Platform** — owns customer-facing charge flows.
- **Ledger & Risk** — owns money movement correctness and fraud controls.

## People

- **Mira Chen** is an engineer on the Payments Platform team. She is the
  primary owner of `payments-api` and is on-call for `payments-api` during
  business hours.
- **Owen Blake** is an engineer on the Payments Platform team. He owns
  `checkout-web` and is secondary on-call for `payments-api`.
- **Priya Nair** leads Ledger & Risk. She owns `ledger-service` and
  `risk-engine`.
- **Sam Ortiz** is on Ledger & Risk. He owns the `ledger-db` datastore and
  wrote the ledger replay runbook.

## Notes for operators

Ownership means pages and merge authority. If an incident names a service,
page the `ONCALL_FOR` / `OWNS` person before paging the whole team.
