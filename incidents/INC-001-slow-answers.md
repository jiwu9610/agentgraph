# INC-001 — "The bot got slow"

*Scenario ticket: this incident was intentionally planted in the instrumented baseline as a production-debugging exercise; reporters are fictional personas. Measured resolution: [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md).*

**Opened by:** Dana Whitfield (Head of Support)
**Severity:** SEV3
**Status:** Resolved — root-cause analysis in [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md)

---

## What was reported

> Filing this because I've now had the same complaint from four different
> merchants this week and I don't have anything useful to tell them.
>
> DocsBot takes forever. Not broken-forever, just... you ask it something and
> you sit there. One of the Tier 2 folks timed it — about two and a half
> seconds before anything comes back. It used to feel instant. I don't know
> when it changed.
>
> The answers are *good*, to be clear. Nobody's complaining about accuracy.
> They're complaining that they've started alt-tabbing away while they wait,
> and then they forget they asked.
>
> Also — and I don't know if this is related, so tell me if I should file it
> separately — infra pinged me Tuesday saying our instances were "flapping"
> after deploys. Something about health checks failing and instances getting
> pulled and replaced, over and over, for about ten minutes after each deploy.
> They asked if DocsBot does anything expensive on startup. I said I have no
> idea, I'm in Support.

**Follow-up from Priya Raman (SRE), same thread:**

> Confirming the flapping. The load balancer health check has a 2 second
> timeout. After a deploy the first probe against a fresh instance times out,
> the LB marks it unhealthy and pulls it, a new one comes up, same thing.
> Eventually it settles. Whatever `/health` is doing, it should not be doing it.
>
> Separately: we ran a small load test this morning — 8 concurrent users, which
> is *nothing*. Per-request latency went from ~2.5s to over 15s. That's not
> what a service under light load should do. It looks like requests are
> queueing behind each other rather than running side by side.

---

## What we know

- p95 for a single `ask` is roughly **2.5s** against the fake provider harness.
- Under 8 concurrent asks, per-request latency degrades **~6x**, which is close
  to fully serial behaviour.
- `/health` is expensive enough to blow a 2s LB timeout on a cold instance.
- Answer quality is not in question. Nobody has reported a wrong answer.

## Action items

Find out where the time actually goes, fix the causes, and prove it with
numbers. Specifically:

1. Get a baseline first — `python -m docsbot.perf.report --asks 20` and
   `--concurrency 8`. Save the output. You cannot demonstrate an improvement
   against a number you never wrote down.
2. Read the `Metrics` stage table. Sort by **self time**, not total time. The
   stage with the biggest self time is where the wall clock is actually being
   spent.
3. Fix the causes. There is more than one, and at least one of them is not in
   the code path you will initially suspect.
4. Verification: `pytest -m phase19`. The suite includes correctness guards,
   which exist because "make it fast by making it wrong" is not a fix.

## Resolution criteria

- p95 for a single ask is under `settings.budget_ask_p95_s`.
- 8 concurrent asks do not degrade per-request latency more than ~2x.
- `/health` makes zero provider calls and returns in single-digit milliseconds.
- You can state, in one sentence each, what the causes were and what each fix
  bought you in milliseconds.
