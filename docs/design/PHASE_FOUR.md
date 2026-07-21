# DocsBot — Phase Four: production debugging

Part one built a RAG bot. Part two made it a product. Part three made it an
agent over a knowledge graph. **Part four is the work you will actually spend
most of your career doing: making something that already works work *well*.**

```
18 Instrument → 19 Latency → 20 Cost → 21 Failure → 22 Leaks + the gate
   spans,          p95, not     tokens,   retries,     TTLs, caps,
   self time,      the mean;    caches,   jitter,      executable
   percentiles     concurrency  batching  breakers     budgets in CI
```

## How part four is different from everything before it

Parts two and three gave you **stubs and failing tests**: the code didn't exist,
you wrote it, the tests told you when you were done.

Part four gives you **working code**.

Every existing test passes. Users get correct answers with correct citations.
There is no `NotImplementedError` anywhere, no blank function bodies, no TODO
pointing at the thing you're supposed to write. The service in
`docsbot/service/` ships today and does its job.

It is also slow, expensive, and wasteful, and there are **four incident tickets
open against it** — written by a support lead, a finance VP, and an SRE, none
of whom know or care what a reranker is. They describe symptoms. You find
causes.

> This is the actual difference between a bootcamp and a job. In a bootcamp the
> problem comes with a spec. At work it comes as "the bot got slow," and the
> first hour of your day is spent figuring out what that sentence means.

**Nothing in this part tells you which line is wrong.** The specs assert on
outcomes — wall clock, provider calls, tokens, dollars — not on implementation.
You get there by measuring.

### The incidents

Read these first. They are in `incidents/`, and they are the assignment.

| Ticket | Reported by | Symptom | Phase |
|---|---|---|---|
| [INC-001](incidents/INC-001-slow-answers.md) | Head of Support | "The bot got slow." Deploys flap. 8 concurrent users → 6x latency. | 19 |
| [INC-002](incidents/INC-002-the-bill.md) | VP Finance | Traffic up 3x. Bill up 12x. Cache hit rate: 0%. | 20 |
| [INC-003](incidents/INC-003-outage-cost-spike.md) | SRE (postmortem) | 3-minute provider blip → 13-minute incident, 30x spend, 90s hangs. | 21 |
| [INC-004](incidents/INC-004-the-sawtooth.md) | SRE | Pods OOM every ~6 hours. Also: nothing stops any of this recurring. | 22 |

### Running the specs

```bash
pip install -e ".[dev]"     # that's all — no key, no network, no extras
pytest                      # earlier parts stay green
pytest -m phase18           # instrumentation
pytest -m phase19           # latency
pytest -m phase20           # cost
pytest -m phase21           # failure under load
pytest -m phase22           # leaks + the regression gate
pytest -m phase4            # all of part four
```

**No API key needed, for any of it.** `docsbot/perf/harness.py` is a
deterministic fake provider that counts every call, prices every token, and
sleeps a realistic amount. Latency is real wall clock. Cost is real arithmetic.
Failures, rate limits, and hangs are injectable.

> ⚠️ The harness is **ground truth** and you must not edit it. Making a budget
> pass by changing the thing that measures you is the debugging equivalent of
> unplugging the smoke alarm. The correctness guards would catch you anyway.

### The tool you debug with

```bash
python -m docsbot.perf.report                 # baseline
python -m docsbot.perf.report --asks 20       # percentiles
python -m docsbot.perf.report --concurrency 8 # does concurrency help?
python -m docsbot.perf.report --turns 10      # conversation cost growth
python -m docsbot.perf.report --index         # what a re-index costs
```

**Run this before you change anything and save the output.** That's your
baseline. Every claim you make later is a diff against it. An optimization you
can't show a before-and-after for didn't happen.

---

## Phase 18 — Instrument it · `docsbot/perf/metrics.py`

**Goal:** build the instrument before you use it. Spans (nested, with **self
time**), counters, **percentiles**, and bounded storage.

**Concepts:** why self time localizes a bottleneck and total time doesn't; why
the mean is a lie and p95 is the truth; why a metrics object that stores every
sample forever is a memory leak that grows fastest under exactly the load you
built it to observe.

**Spec:** `pytest -m phase18` — pure logic, no provider.

**Why it matters:** every instinct you have about where a program spends its
time is wrong. Reliably, not occasionally. The engineers who are good at this
aren't the ones with better instincts — they're the ones who stopped trusting
instinct and measured.

## Phase 19 — Latency · closes INC-001

**Goal:** p95 under `settings.budget_ask_p95_s`, concurrency that actually
buys you something, and a health check that isn't secretly the most expensive
endpoint you own.

**Concepts:** serial round-trips vs. batched or concurrent ones; work on the
critical path that didn't need to be there; global locks that quietly turn a
concurrent service into a queue; why a liveness probe must never trigger cold-
start work.

**Spec:** `pytest -m phase19`

**Note:** this phase does not care *how* you kill the serial round-trips —
batching and parallelising both pass. Phase 20 will care, for a different
reason.

## Phase 20 — Cost · closes INC-002

**Goal:** cut cost per ask several-fold without touching answer quality.

**Concepts:** caches that are correctly wired and never hit; checking a cache
*after* doing the work it was meant to avoid; N+1 calls to an endpoint that
takes batches; sending a whole document where retrieval already picked the
paragraph; resending an entire transcript every turn (quadratic cost, then a
context-window failure); paying flagship prices for a one-word answer.

**Spec:** `pytest -m phase20`

**The lesson underneath:** *fewer calls beats faster calls.* Parallelising eight
calls fixes latency and changes cost by exactly nothing. Batching them into one
fixes latency **and** cost **and** your rate-limit headroom.

## Phase 21 — Failure and waste under load · closes INC-003

**Goal:** make the next three-minute provider blip a three-minute provider blip.

**Concepts:** retrying what cannot succeed; deterministic backoff as a
synchronization bug (you built a metronome, and the 20x spike is the herd
arriving together); no timeout as the reason workers park on dead sockets and
take down requests that were fine; the circuit breaker as the state that says
"stop calling, it's down"; and choosing the right *unit* of retry, because
retrying a batch re-pays for every item that already succeeded.

**Spec:** `pytest -m phase21` — instant, because `sleep` is injected.

## Phase 22 — Leaks and the regression gate · closes INC-004

**Goal:** flatten the memory sawtooth, then make all four fixes permanent.

**Concepts:** configured limits vs. enforced limits (a TTL nothing ever applies
is decoration); why a cap is needed even with a TTL; bounding your own
instrumentation; and turning measured numbers into an executable budget that
fails the build.

**Spec:** `pytest -m phase22`

**Deliverable:** `POSTMORTEM.md` in the repo root, from
[the template](incidents/POSTMORTEM_TEMPLATE.md). This is graded like code. The
writeup is how four fixes stop being folklore and start being something the
next engineer can act on — including the wrong guesses, which are the most
useful part for the next reader.

---

## Suggested order & definition of done

1. **18** — build the cockpit. Don't skip ahead; everything else depends on
   being able to see.
2. **Baseline.** Run the report. Save it. Read all four incidents.
3. **19 → 20 → 21 → 22**, in order. Each closes one ticket.
4. **Write the postmortem** as you go, not at the end. You will not remember
   your wrong guesses a week later, and those are the valuable part.

A phase is done when its `pytest -m phaseN` is fully green — **including the
correctness guards**, which exist because the fastest possible DocsBot returns
`""` instantly for every question and costs nothing. You are being asked to make
a *working* product cheap. That is a much harder and much more useful problem
than making a broken one fast.

## What you should be able to do afterwards

- Take "the app feels slow" and turn it into a ranked list of causes with
  numbers attached, without guessing.
- Know the difference between latency work and cost work, and when a fix
  addresses one and not the other.
- Read a retry policy and see the bill it will produce during an outage.
- Recognize a bounded resource from an unbounded one on sight.
- Write the postmortem that stops the same problem recurring after you leave.

## Stretch (after 22)

- Wire the budget gate into `.github/workflows/ci.yml` so a regression fails the
  PR, and make the failure message name the metric and the overage.
- Add a `--compare baseline.json` flag to `report.py` so it prints deltas
  against a saved run instead of you diffing two terminal windows by eye.
- Track cost per *user*, not just per process, and find the query pattern that
  costs the most. In real products it's rarely the one you'd guess.
- Add a semantic cache: serve a cached answer when a new question is close
  enough in embedding space to one you've already answered. Then work out how
  you'd ever know it was returning something subtly wrong.
- Load-shed instead of queueing: above a concurrency threshold, return a fast
  "try again" rather than accepting work you can't finish in time.
