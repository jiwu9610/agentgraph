# INC-004 — The sawtooth

*Scenario ticket: this incident was intentionally planted in the instrumented baseline as a production-debugging exercise; reporters are fictional personas. Measured resolution: [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md).*

**Opened by:** Priya Raman (SRE)
**Severity:** SEV3 (chronic)
**Status:** Resolved — root-cause analysis in [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md)

---

## What was reported

> Attaching a screenshot of the memory graph for the DocsBot pods over the last
> week. I'd like you to look at it before reading anything else, because the
> shape tells you most of the story.
>
> It's a sawtooth. Memory climbs steadily for about six hours, hits the limit,
> the pod gets OOM-killed, restarts at baseline, and climbs again. Seven times a
> day, every day. It has been doing this since launch.
>
> Users mostly don't notice — a request in flight during a kill fails, they
> retry, it works. So this has quietly sat at the bottom of the backlog for
> months, which is exactly how a chronic problem becomes permanent.
>
> Two things make me want it fixed now:
>
> 1. The climb rate scales with traffic. We're growing. At current growth the
>    six-hour cycle becomes a ninety-minute cycle by roughly Q3, and at some
>    point "users mostly don't notice" stops being true.
> 2. It restarts most often during our busiest hours, because that's when it
>    fills fastest. The failure mode is precisely correlated with the traffic
>    we most care about serving.
>
> I did some poking. The session store looks like the obvious suspect — it's
> configured with a TTL and a max size, and as far as I can tell neither one is
> ever actually applied to anything. There's a `purge_expired` method. Nothing
> calls it. It's been dead code since it was written.
>
> I also want someone to look at whatever the instrumentation is doing. If we
> just built a metrics object that appends a sample per request and
> never drops any, we've added a *second* leak that grows with traffic, in the
> code whose entire job is to tell us about problems like this. That would be a
> hell of a thing to page ourselves about.

**Follow-up from Sam Ortiz (Eng Manager), same thread:**

> Adding scope, and I realize this is the less urgent half, but it's the half I
> actually care more about.
>
> We've now fixed three of these — latency, cost, and the retry storm. In every
> case somebody measured, found the cause, fixed it, and moved on. Nothing we
> did stops any of it from coming back. The next person to add a call inside a
> loop, or send a whole document where a chunk would do, will not get any
> signal from CI. They'll find out from Marcus, in eight weeks, in a thread
> about the invoice.
>
> I don't want a wiki page with our target numbers on it. We had one of those.
> I want the numbers to be a test that fails the build.

---

## Action items

Two halves.

**Stop the leaks.** The session store must actually enforce its TTL and its
cap. Your metrics object must bound its own storage. Anything that grows
without limit as traffic grows is a leak, including — especially — the tooling
you added to find leaks.

**Build the gate.** Turn the numbers you've been chasing into an executable
budget that runs in CI and fails the build on regression. Set each budget at
today's measured value plus modest headroom. A budget with 10x slack never
fires, including when it should. A budget with no slack fires on CI noise and
gets disabled within a week, which is worse than not having one. Aim for
roughly 20–30%.

Then write the postmortem, using the template in this directory. The writeup
is how the four fixes stop being folklore and become something the next
engineer can act on.

## Resolution criteria

(Verification: `pytest -m phase22`.)

- The session store's TTL and cap are enforced under load, not just configured.
- `Metrics` storage is bounded regardless of how many samples arrive.
- `docsbot/perf/budgets.py` is implemented and the gate catches a regression.
- The postmortem covers all four incidents: what the symptom was, what the
  cause was, what the fix was, and what number proves it.
