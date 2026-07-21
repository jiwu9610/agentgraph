# Northstar Cloud — Cost Management

Payments is thin margins on high volume, so infrastructure cost per transaction
is a product metric, not a finance afterthought.

## Unit economics

The number we manage is **fully loaded infrastructure cost per authorized
transaction**, currently **$0.0021** — compute, storage, network, observability,
and tooling over authorized transactions. It excludes processor fees, which pass
through and are tracked separately.

Our internal ceiling is $0.0030. Above that, unit margin on our smallest
merchant tier goes negative and growth makes it worse. The target is cost per
transaction flat or falling while volume grows, so rising absolute spend is not
by itself a problem. Reviewed monthly, reported quarterly.

## Where the money goes

By share of infrastructure spend:

- **PostgreSQL: 34%.** Provisioned IOPS dominates; the ledger's synchronous
  replication is the largest single line item and is not negotiable.
- **Observability: 22%.** Fastest-growing, and most out of proportion to value.
- **Compute: 21%**, mostly payments-api and risk-engine, provisioned for peak.
- **Kafka: 11%**, driven by partition count and the 7-day retention.
- **Network egress: 7%**, mostly cross-AZ replication and webhooks.
- **Redis and everything else: 5%.**

High-cardinality metric labels are the most common cause of a sudden cost jump:
adding merchant id to a frequently emitted metric has twice multiplied the
metrics bill overnight. Merchant id belongs in traces, not labels.

## Processor fees and routing

Processor fees are an order of magnitude larger than infrastructure cost, so
routing matters more than any server we might resize. Structures vary:
interchange-plus with a per-transaction markup, blended flat rates, and
volume-tiered pricing with monthly minimums.

Routing rules are per merchant and weigh fee structure, authorization rate, and
settlement speed. A processor 4 basis points cheaper that approves 1.5% fewer
transactions is far more expensive, because a decline loses the whole sale.
Evaluate routing on net revenue per attempt, never on fee alone. Fallback
routing changes the fee structure, so it requires merchant opt-in.

## Catching cost regressions in CI

Every pull request runs a cost regression check. It estimates the per-transaction
cost change from three signals: query plans for new SQL, the cardinality of new
metric labels, and new outbound calls per request path.

The gate warns at an estimated +3% and fails at +10%. A failure can be merged
with written justification on the PR, because the estimator is coarse — the
point is to force a conversation, not to be an oracle. A new N+1 query is the
most common failure. A new synchronous call in the authorization path fails
regardless of cost, since it needs an architecture review anyway.

## Budget alerts and ownership

Each service has an owning team and a monthly budget. Alerts fire at 80% of
budget projected by month end and again at 100%, to the owning team's channel
rather than a finance list — a cost alert with no engineering owner is ignored.

A platform-wide anomaly alert fires on any single-day spend more than 25% above
the trailing 14-day median, catching accidental over-provisioning and runaway
backfills within hours. Any vendor above $25,000 a year needs platform lead
approval.

## Over-provisioning versus incident risk

We deliberately over-provision the authorization path. payments-api and
risk-engine run at roughly 40% steady-state CPU so a regional failover, which
doubles load on the surviving region, needs no scale-up mid-incident.

That headroom costs about 8% of infrastructure spend, and it is cheap: one hour
of degraded authorization costs more than a year of it.

reporting-api, settlement-worker, and webhook-dispatcher may queue and catch up,
so they run leaner and scale reactively. But do not cut authorization-path
headroom to hit a cost target: if cost per transaction is over the ceiling, look
at observability and query efficiency first.

## Caching and batching as levers

Caching is our highest-leverage cost tool, with one constraint: Redis holds only
reconstructible state — rate-limit counters, the API key cache, idempotency
keys. Any cache must be safe to lose; cold Redis makes the platform slower and
more permissive, never wrong.

The API key cache alone removes roughly 90% of authentication reads from
payments-api's database. A merchant config cache with a 60-second TTL removed
much of the rest; that TTL means config changes take up to a minute to apply.

settlement-worker already batches by processor schedule. Ledger writes are the
exception: batching them would delay authorization, and ledger-service is the
one hard synchronous dependency in that path, so we pay for the latency
guarantee on purpose.

For asynchronous work, prefer batches of 500 to 1,000 records throttled to keep
replication lag under 5 seconds. Large unthrottled backfills are our most common
self-inflicted incident.
