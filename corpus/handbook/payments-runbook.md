# Northstar Cloud — Payments Runbook

Operational runbook for the authorization and settlement path. Assumes the
platform architecture doc.

## Alerts and what they mean

**AuthFailureRateHigh** — decline rate across all merchants above 12% over 5
minutes, against a baseline of 6-8%. The most common page, and usually not our
fault. Check the per-processor breakdown first.

**AuthFailureRateHighSingleMerchant** — 35% declines for one merchant over 10
minutes with at least 50 attempts. Almost always a merchant bug or carding.
SEV3; notify the account owner.

**ProcessorLatencyHigh** — p99 adapter latency above 3s for 5 minutes. Our client
timeout is 8s, so this is early warning, not outage.

**LedgerWriteLatency** — p99 above 250ms. ledger-service is the one hard
synchronous dependency in the authorization path, so treat this as a precursor
to a full authorization outage. Investigate immediately.

**IntentOrphanBacklog** — more than 500 intents in `pending` for over 15 minutes.
A few orphans are expected; a backlog means payments-api is crashing mid-flight,
or reconciliation is stuck.

**SettlementBatchStalled** — a batch in `submitting` more than 45 minutes past
its processor's window.

**RiskEngineTimeoutRate** — over 5% of scoring calls exceeding the 40ms budget.
risk-engine fails open below each merchant's configured floor, so this is about
fraud exposure, not availability. Never a SEV1.

## Diagnosing elevated authorization failures

Work in this order; each step eliminates the largest remaining cause.

1. Split by processor. If one accounts for the spike, it is a processor incident.
   Check their status page, then consider fallback.
2. Split by decline reason code. A jump in `do_not_honor` or `insufficient_funds`
   is issuer-side and usually geographic. `invalid_request` is ours or the
   merchant's.
3. Split by merchant. If the top three explain the spike, it is not a platform
   incident.
4. Check whether risk-engine hard-declines rose. The model refreshes nightly; a
   bad model shows as a step change at a fixed hour with no processor
   correlation. Roll back to the previous model artifact — a config change, about
   90 seconds.
5. Only then look at payments-api: error logs, intent write failures, restarts.

Fallback routing is per merchant and disabled by default because it changes the
merchant's fee structure. Enable it during a confirmed processor outage only for
merchants who pre-approved fallback in their routing config. Enabling it
otherwise is a contract violation.

## Processor timeouts

The adapter timeout is 8 seconds with no automatic retry. This is deliberate: a
timeout means we do not know whether the processor authorized the payment, and
blind retries create double charges. The intent row is already written, so leave
it `pending` and let reconciliation query the processor for the true state.
Reconciliation runs every 2 minutes and resolves most timeouts within 5.

If a processor times out on more than 20% of calls, stop sending it traffic. The
circuit breaker opens automatically at 25% failures over 60 seconds, half-opening
after 30 seconds.

## Replaying a stuck settlement batch

settlement-worker is idempotent on batch id, which makes replay safe.

1. Confirm the batch's terminal state with the processor directly, not from our
   database. Processors sometimes acknowledge a batch twice, so our "submitted"
   record may be a duplicate ack.
2. Never generate a new batch id for a replay; that defeats idempotency and will
   double-settle.
3. Run `settlement-cli replay --batch-id <id> --dry-run`; read the diff.
4. Run without `--dry-run`. A correct replay of an already-settled batch produces
   zero new ledger entries.

If the dry run shows an unexpected money difference, stop and escalate rather
than making the numbers match.

## What not to do

Never update or delete a ledger entry. The ledger is append-only and corrections
are compensating entries referencing the original. Running an UPDATE against the
ledger database causes a bigger incident than the one you were fixing.

Never mass-void intents to clear an alert. The backlog is a symptom; voiding
intents destroys evidence and may void payments the processor approved.

Never disable reconciliation to reduce load. It is the safety net for every
processor timeout in flight.

Never bypass idempotency checks to let a merchant retry. Redis idempotency keys
are a performance dependency; the PostgreSQL uniqueness constraint is the
correctness one.

## Escalation

Page the payments on-call lead if authorization success rate is below 85% for
more than 10 minutes, if ledger-service is unavailable for any duration, on any
suspected double-charge affecting more than one merchant, or whenever a fix
involves writing to a database by hand.

Page finance leadership within 30 minutes for any confirmed settlement
discrepancy over $50,000, cause known or not. Their external reporting
obligations start before we finish the fix.
