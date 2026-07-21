# Northstar Cloud — Deployment and Rollback

We deploy often because small changes are easier to reason about when they
break.

## CI gates

Every pull request must pass, in order: unit tests, integration tests against
ephemeral PostgreSQL and Kafka, contract tests against recorded processor
adapter fixtures, a migration linter, and a cost regression check. An override
needs a second engineer's approval recorded on the PR.

Test suites must run in under 12 minutes; a slow suite gets skipped in spirit
long before anyone admits to skipping it. Contract tests are the gate people
most want to weaken, because fixtures go stale — refresh them weekly instead of
loosening assertions.

## Canary stages and bake times

A normal deploy moves through four stages: **1%** of traffic for 10 minutes,
**10%** for 20 minutes, **50%** for 30 minutes, then **100%**.

Bake times are minimums. Canary analysis compares error rate, p99 latency, and
per-endpoint success rate against the stable version and halts promotion on a
significant regression. A halted canary rolls back automatically rather than
sitting at 10%.

The 1% stage catches crash loops and config errors; the 50% stage catches
contention that only appears under real load. Skipping stages for a one-line
change removes the stage that catches what one-liners actually break.

## ledger-service is different

ledger-service is the one hard synchronous dependency in the authorization path,
and it is append-only, so its mistakes are permanent. Its rules:

- Deploys only Monday to Thursday, 10:00–16:00, with on-call aware.
- Two-person review; the second reviewer must be on the ledger team.
- Canary stages of 1% / 5% / 25% / 100% with bake times of 30 / 60 / 60 minutes
  — roughly two and a half hours end to end.
- Any change to entry-writing logic requires a shadow run: the new path computes
  entries alongside the old for at least 24 hours in production, and results are
  diffed. Zero diffs is the bar.
- No ledger deploy during any active SEV2 or higher, anywhere.

Slower on purpose. Every other service can be fixed forward; a wrong ledger
entry can only be compensated.

## Feature flags versus deploys

Ship code dark, then turn it on. Changing behavior in the same push that changes
code leaves one blunt way to undo it.

Flags evaluate per merchant and per percentage. A flip takes effect within 30
seconds and needs no deploy, which is why it is the preferred incident
mitigation.

Flags are not free. Every flag gets an owner and a removal date at creation, with
a default life of 90 days; older ones appear on a weekly report to the owning
team. A permanent flag is really a configuration option and should be promoted
to one, with validation.

Never gate a migration behind a flag. Schema is not a runtime decision.

## Rolling back

Rollback is redeploying the previous known-good image. It takes under 4 minutes
for any service. If you are wondering whether to roll back, roll back.

Rollback is wrong in three cases:

- The new version already wrote data the old cannot read. Follow the migration
  rules below and this does not happen.
- The bad change is in a nightly artifact rather than code, such as a
  risk-engine model refresh. Roll back the artifact, not the service.
- The previous version has a known worse bug. Forward-fix is then safer, but the
  incident commander decides that explicitly and time-boxes it: if the fix is
  not in production within 30 minutes, roll back anyway.

## Database migrations

We use expand/contract, always, across at least three deploys:

1. **Expand.** Add the column, table, or index — nullable, with a default that
   does not rewrite the table. No code reads it yet.
2. **Dual-write.** New code writes both shapes, reads the old. Backfill runs in
   batches, throttled to keep replication lag under 5 seconds.
3. **Switch reads**, verify, then **contract** later: drop the old column.

The absolute rule: never ship a destructive migration in the same deploy as the
code that depends on it. If the code rolls back the schema does not, leaving a
service that reads a column which no longer exists. Leave at least one full
deploy cycle — in practice 24 hours — between the two.

Index creation is always `CONCURRENTLY`. Any migration expected to hold a lock
for over 1 second needs a written plan, plus ledger-team review if it touches
ledger tables.

Migrations are forward-only: an untested down migration is a comforting fiction.
Undo a schema change with a new migration.

## Deploy freezes

Freezes are automatic during the last three business days of each month, when
settlement volume peaks; Black Friday through the Tuesday after Cyber Monday;
and any platform-wide SEV1.

During a freeze only two things ship: incident mitigations, and security fixes
rated high or critical. Both need incident commander or on-call lead approval,
recorded in the channel.

Freezes do not block flag changes — another reason to keep risky behavior behind
a flag.
