# INC-003 — Provider blip, 30x spend, 90-second hangs

*Scenario ticket: this incident was intentionally planted in the instrumented baseline as a production-debugging exercise; reporters are fictional personas. Measured resolution: [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md).*

**Opened by:** Priya Raman (SRE) — postmortem action item
**Severity:** SEV1 (resolved; follow-up work open)
**Status:** Resolved — root-cause analysis in [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md)

---

## Timeline (from the postmortem)

**14:02** — Model provider posts "partial degradation" on their status page.
Roughly 40% of requests start returning 429 and 503.

**14:02–14:05** — The provider is degraded for **three minutes**.

**14:03** — Our request latency goes from 2.5s to 90s+. Not errors. Hangs. Users
sit on a spinner. Support starts getting messages.

**14:04** — Our outbound call volume to the provider increases roughly **20x**
while the provider is *less* available, not more. We are, in effect, DDoSing a
service that is already having a bad day.

**14:07** — Provider recovers on their side. Our error rate stays elevated for
another **six minutes** because we are still working through a backlog of
retries we generated ourselves.

**14:13** — Recovered.

**Cost of the incident:** approximately **30x** our normal hourly spend, for a
three-minute upstream event, and we served fewer successful requests during it
than we would have if we'd simply returned errors immediately.

---

## What Priya wrote in the postmortem

> The upstream blip is not the interesting part. Providers have bad days; that's
> expected and it's why we have a retry policy. The interesting part is that our
> retry policy converted a three-minute partial degradation into a thirteen-minute
> full incident, at 30x cost, and made the upstream problem worse while it was
> happening.
>
> Four things I want fixed before the next one:
>
> **We retried things that cannot succeed.** A meaningful share of that call
> volume was 400s. A malformed request retried four times is four identical
> failures and four times the quota. We should fail those instantly.
>
> **Every client retried in lockstep.** Our backoff is deterministic, so every
> in-flight request that got rate-limited at 14:03 computed the same delay,
> slept the same duration, and hit the provider again *at the same
> millisecond*. Then again. Then again. That's the 20x spike — it isn't
> volume, it's synchronization. We built a metronome.
>
> **Nothing ever gave up on the provider.** For eleven minutes we kept sending
> full traffic at a service we had overwhelming evidence was not answering.
> There is no state anywhere in our code that represents "the provider is
> down, stop calling it." We need one.
>
> **Nothing timed out.** This is where the 90 seconds came from. Some calls
> weren't failing, they were *hanging* — the provider accepted the connection
> and never answered. Our workers parked on those sockets, which meant users
> whose requests were completely fine also got nothing, because there was no
> worker free to serve them. An error at 2 seconds is a vastly better product
> than a spinner at 90.
>
> One more, from reading the code afterward: our batch retry helper retries the
> **whole batch** when any single item fails. During the incident that meant one
> failed item in a batch of fifty re-paid for the forty-nine that had already
> succeeded. With a 40% failure rate, large batches were close to never
> completing at all.

---

## Action items

Make the next three-minute provider blip a three-minute provider blip.

You have the seams already: `sleep` is injected, so you can test a retry
policy without waiting for one. The harness can inject 429s, 400s, and hangs
on demand — see `FakeProvider.fail_next()` and `hang_next()`.

## Resolution criteria

(Verification: `pytest -m phase21`.)

- A non-retryable error costs exactly **one** provider call.
- Persistent failures are bounded by `settings.max_retries` and then give up.
- Backoff is jittered — two clients failing simultaneously do not retry in
  lockstep.
- A hanging call raises a timeout instead of blocking forever.
- After `settings.breaker_threshold` consecutive failures the circuit opens and
  further calls fail fast **without touching the provider**.
- One failing item in a batch does not re-run the items that already succeeded.
- Total spend during a simulated outage is bounded, and you can state the bound.
