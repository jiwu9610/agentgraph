# Northstar Cloud — Data Retention and Handling

What we keep, for how long, and what we never store. Each rule came from a
regulator or a postmortem.

## Retention schedule

**Ledger entries: permanent.** Never deleted, never archived out of queryable
storage, never modified — the ledger is append-only and corrections are
compensating entries referencing the original. Records must be reproducible for
the full statutory period, up to 10 years by jurisdiction, and ledger storage is
under 3% of platform data cost anyway.

**Payment intents: 7 years, then anonymized rather than deleted.** The row keeps
amount, currency, timestamps, status, and processor, and loses the customer
reference, device fingerprint, and IP. The shell stays because settlement and
ledger reconciliation walk these rows.

**Risk features and scores: 24 months** — long enough to retrain and to
investigate chargebacks, which arrive up to 540 days later.

**Webhook delivery records: 90 days.** Dead-lettered events keep the same 90
days, replayable from the dashboard — separate from webhook-dispatcher's
24-hour retry window before dead-lettering.

**Reporting aggregates: 25 months.**

**Kafka topics: 7 days.** Kafka is transport, not storage. Anything that must
outlive a week is written to PostgreSQL by a consumer.

## PII and tokenization

Personal data lives in exactly one place per category and is referenced by token
everywhere else. Customer identity — name, email, billing address — lives in the
vault behind a token of the form `cus_tok_*`. payments-api, risk-engine, and
reporting-api hold tokens, not values, so a compromise of the payments-api
database yields transaction shapes, not identities.

The vault has its own credentials, an audit log of every detokenization, and
per-service rate limits. Bulk detokenization needs a signed request.

Never log a detokenized value — not in errors, Kafka messages, metric labels, or
support tickets.

## Card data and the PCI boundary

Northstar is PCI DSS Level 1. The cardholder data environment is deliberately
tiny: the card vault and the processor adapters. Nothing else sees a PAN.

Cards enter through a client-side library that posts directly to the vault,
which returns a network or vault token. That keeps audit scope small, which is
why routing card data through a general-purpose service gets refused.

We never store CVV — not encrypted, not briefly, not for a retry; it is
forwarded to the processor and discarded. Any change touching the cardholder
data environment needs security team review before merge.

## Logs

Application logs are retained 30 days hot, then 12 months in cold storage. Audit
logs — authentication, key issuance, vault detokenization,
permission changes, ledger writes — are retained 7 years in append-only storage
and cannot be deleted by any application credential.

Redaction happens in the logging library, not at the query layer. It strips
known-sensitive field names and matches PAN-shaped and email-shaped patterns
before anything leaves the process, because filtering after ingestion means the
data was already stored. Sensitive data in the logs is a security incident, not
a cleanup task.

Redis holds only rate-limit counters, the API key cache, and idempotency keys —
never retained or backed up, because all of it is reconstructible.

## Merchant export and deletion

Merchants export their own data from the dashboard at any time: transactions and
payouts as CSV, up to 25 months, as a signed link valid for 24 hours. Larger
exports are a support request with a 5 business day target.

Deletion requests — including end-customer requests forwarded under GDPR or
CCPA — are honored within 30 days and mean deletion of personal data, not
financial records. We purge the vault entry and anonymize the referencing rows;
the transaction and its ledger entries survive without identity attached. The
data processing agreement says so, because "delete everything" and "keep
required financial records" cannot both be satisfied. Offboarding is the same:
keys revoked immediately, vault entries purged once any dispute window closes,
ledger history retained.

## Backups and restore testing

Every PostgreSQL database takes a nightly full backup plus continuous WAL
archiving, giving point-in-time recovery to any second in the last 35 days.
Backups are encrypted with a key we hold, stored in a second region, retained 35
days for dailies and 13 months for month-end snapshots.

The ledger database also streams synchronously to a standby in a second
availability zone, so its recovery point objective is zero. Every other database
has an RPO of 5 minutes and an RTO of 1 hour.

Restores are tested monthly into an isolated environment, and a test is not a
success until an engineer runs a real query against the restored data and gets
the expected answer — a backup never restored is a hypothesis. Quarterly, one
runs as a game day with the on-call rotation.
