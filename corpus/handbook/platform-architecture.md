# Northstar Cloud — Platform Architecture

Northstar Cloud is a payments platform. Merchants integrate once and we handle
authorization, capture, settlement, refunds, and reporting across a dozen
downstream processors. This document is the map: what the services are, what
they own, and which failures are survivable.

## Service inventory

**edge-gateway** terminates TLS, authenticates API keys, applies per-merchant
rate limits, and routes to the service that owns the request. It holds no
business state. Because it is the only path in, an edge-gateway outage is a
total outage; it runs three replicas per region with no shared dependencies
beyond the key cache.

**payments-api** is the front door for authorization and capture. It validates
the request, calls risk-engine for a decision, then calls the appropriate
processor adapter. It writes an intent row before calling anything external, so
a crash mid-flight is recoverable — the intent is the source of truth for what
we were trying to do, and the ledger is the source of truth for what happened.

**risk-engine** scores each transaction from merchant history, device
fingerprint, velocity counters, and a gradient-boosted model refreshed nightly.
It answers in under 40ms at p99 or payments-api proceeds without it, treating a
risk-engine timeout as a soft approve for transactions under the merchant's
configured floor. This fail-open behavior is deliberate and reviewed quarterly:
blocking all payments because a scoring service is slow costs more than the
fraud we would have caught in those seconds.

**ledger-service** is the double-entry accounting core. Every movement of money
produces balanced debit and credit entries. It is append-only. Nothing in the
system may update or delete a ledger entry; corrections are new compensating
entries that reference the original. This is the one service where we accept
worse latency for stronger guarantees.

**settlement-worker** batches captured transactions and submits them to
processors on each processor's schedule, which varies from every 15 minutes to
once daily. It is idempotent on batch id, because processors occasionally
acknowledge a batch twice.

**reporting-api** serves merchant dashboards from a read replica that lags the
primary by up to 90 seconds. Merchants are told this in the docs, and the
dashboard shows a "as of" timestamp, because a merchant who thinks a payment
vanished will call support within four minutes.

**webhook-dispatcher** delivers event notifications to merchant endpoints with
exponential backoff over 24 hours, then moves the event to a dead-letter queue
that merchants can replay from the dashboard.

## Data stores

The primary datastore is PostgreSQL, one logical database per service, with no
cross-service joins. Services communicate over HTTP with JSON, and asynchronous
work moves through Kafka topics partitioned by merchant id, which preserves
per-merchant ordering without forcing global ordering.

Redis holds three things: rate-limit counters, the API key cache, and
idempotency keys. All three are reconstructible, which is why Redis is a
performance dependency and not a correctness dependency. If Redis is cold, the
platform is slower and more permissive, not wrong. Any change that puts
non-reconstructible state in Redis needs an architecture review.

The ledger's PostgreSQL instance uses synchronous replication to a standby in a
second availability zone. Every other database uses asynchronous replication.
The distinction is intentional: we can tolerate losing a few seconds of
reporting data in a zone failure, but not a few seconds of ledger entries.

## Request flow for an authorization

A merchant POSTs to `/v1/payments`. edge-gateway authenticates and rate-limits,
then forwards to payments-api. payments-api validates the payload, checks the
idempotency key in Redis, and writes a `payment_intent` row with status
`pending`. It calls risk-engine, then the processor adapter selected by the
merchant's routing rules. On a processor approval it writes ledger entries
through ledger-service, updates the intent to `authorized`, and emits a
`payment.authorized` event to Kafka. webhook-dispatcher picks that up and
notifies the merchant.

The critical property is that the intent row is written before any external
call. If payments-api dies after calling the processor but before recording the
result, a reconciliation job finds the orphaned intent, queries the processor
for the true state, and completes or voids it. Without that row we would have
taken a merchant's customer's money with no record that we tried.

## Failure domains

A processor outage degrades only merchants routed to that processor; routing
rules support a fallback processor per merchant, disabled by default because
fallback changes the fee structure and merchants must opt in.

A risk-engine outage is a soft degradation, as described above.

A ledger-service outage stops authorization entirely. We cannot approve a
payment we cannot record. This is the one hard dependency in the authorization
path, and it is why ledger-service has the strictest deployment rules and the
most conservative rollout policy of any service we run.

A Kafka outage stops webhooks and settlement but does not stop authorization;
events accumulate in the outbox table and drain when Kafka returns.

## Deliberate constraints

We do not do distributed transactions. Every multi-service operation is either
idempotent and retryable, or it writes an intent first and reconciles later.

We do not read from another service's database. Ever. The read replica for
reporting is owned by reporting-api and populated by change-data-capture, not
by pointing reporting-api at the payments database.

We do not add a synchronous dependency to the authorization path without an
architecture review, because every one we add multiplies into our availability
budget. Authorization currently depends synchronously on exactly two services,
ledger-service and the processor adapter, and that number is a design goal, not
an accident.
