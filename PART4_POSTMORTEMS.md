# Part 4 — Production debugging: postmortems and study guide

**What this is.** Part 4 shipped a *working* LLM service (`docsbot/service/`) with planted
performance pathologies and four incident tickets written by non-engineers. The specs
assert on outcomes — wall clock, provider calls, tokens, dollars — never on implementation.

**Attribution, plainly.** The fixes on this branch are the mentor's reference solution,
merged from `origin/part4-solutions` (see git history). What is ours: the before/after
measurements below (taken on identical compute nodes), and this analysis — every root
cause traced from the ticket's symptom to the exact before/after code, verified line by
line against the diff. The goal of this document is that the reader can explain each
incident cold: what the user saw, how you would measure it, what was actually wrong, why
the fix works, and what tempting fix would not have.

## The scoreboard (measured, not estimated)

| Metric | Before (broken) | After (fixed) | Change | Budget |
|---|---|---|---|---|
| Indexing: provider calls (49 chunks) | 49 | 1 | 49× fewer | — |
| Indexing: wall clock | 0.746 s | 0.025 s | 30× faster | — |
| Ask p95 latency (serial) | 0.273 s | 0.090 s | 3× faster | 0.130 s ✓ |
| Ask p95 latency (8 concurrent) | 1.416 s | 0.133 s | 10.6× faster | 0.130 s (borderline) |
| Total wall, 5 asks @ concurrency 8 | 1.420 s | 0.135 s | 10.5× | — |
| Provider calls per ask | 11 (8 = rerank) | 4 | 2.75× fewer | 4 ✓ |
| Input tokens per ask | 7,553 | 2,529 | 3× fewer | 3,200 ✓ |
| Cost per ask | $0.000653 | $0.000267 | 2.4× cheaper | $0.000340 ✓ |
| Turn-10 conversation tokens | 10,991 (rising from 8,313) | 3,277 (flat) | 3.4× fewer, no growth | — |
| Self-time breakdown available | no (Phase 18 unbuilt) | yes | — | — |
| Part 4 specs | 24 fail / 62 unrun | 94/94 pass | — | — |

Sources: [`docs/measurements/`](docs/measurements/) — `baseline-before.txt`,
`baseline-before-extra.txt`, `after-fixes.txt`
(the "code under test" line in `baseline-before-extra.txt` pins the exact checkout that
ran — the editable install can silently fall back to the main checkout, and did once
during this work).

## How to study this

Read one incident per sitting. For each: (1) read the ticket in `incidents/` first — it is
written in the reporter's words; (2) say out loud how you would *measure* it before looking
at code; (3) read the root causes; (4) answer the interview questions without the text.
The one-line story at the top of each section is what you should be able to produce from
memory.

---

## Phase 18 — Instrumentation (the cockpit): docsbot/perf/metrics.py

**Incident:** Phase 18 (no ticket: build the instrument). Indirectly feeds INC-001 (p95 latency, Phase 19) and INC-004 (the sawtooth, Phase 22: "whatever the instrumentation is doing").

> **One-line story:** Before Phase 18 every function in docsbot/perf/metrics.py raised NotImplementedError and the report could only print "Phase 18 not built yet" -- so nobody could say which stage was slow; the fix is a thread-safe Metrics with per-thread nested spans that compute SELF time (duration minus children), locked counters, nearest-rank p50/p95 instead of the mean, a reset() for clean baselines, and a drop-oldest cap of 4096 samples per metric so the instrument cannot itself become INC-004's leak; result: `pytest -m phase18` green and `python -m docsbot.perf.report` prints a stage table sorted by self_s that points at rerank's serial provider loop, the 0% cache hit rate, and a p95 you can put a budget on.

### What was reported

There is no reporter for this phase because there is nothing to report yet: the service works, every test passes, and four tickets (INC-001..004) describe symptoms ("the bot got slow", "bill up 12x", "pods OOM every ~6 hours") without anyone being able to say which stage is responsible. Every function in origin/part4:docsbot/perf/metrics.py raises NotImplementedError, so `python -m docsbot.perf.report` prints "[ Phase 18 not built yet — build Metrics to see this table ]" (report.py:154) where the per-stage breakdown should be, and the service silently runs with `_NullMetrics` (pipeline.py:70-81; `span()` returns `nullcontext()`). The closest thing to a reported symptom is Priya Raman's line in incidents/INC-004-the-sawtooth.md:38-42: "If we just built a metrics object in Phase 18 that appends a sample per request and never drops any, we've added a *second* leak that grows with traffic, in the code whose entire job is to tell us about problems like this. That would be a hell of a thing to page ourselves about."

### How you measure it

Acceptance is `pytest -m phase18` (24 pure-logic tests in tests/test_phase18_instrumentation.py, no provider, no key). Once green, `python -m docsbot.perf.report --asks 20` prints "where the wall clock went (self time = not in a child span)" with columns stage/count/total_s/self_s/p95_s, sorted by self_s descending (report.py:157 `rows = sorted(table.items(), key=lambda kv: -kv[1]["self_s"])`). The number you look at is the top row's `self_s` -- that is the stage to fix first. Note that in the real pipeline the `rerank` span wraps the whole candidate loop (pipeline.py:201), so its `count` is 1 per ask; the N+1 shows up as rerank's large `self_s` plus the provider's `calls_by_tag` line (report.py:147) showing ~8 `rerank` calls per ask. Also read the "p95 latency ... (budget settings.budget_ask_p95_s)" line (report.py:136), which is nearest-rank p95 over per-ask wall clock. For the bounded-storage requirement the number is `len(m.samples(name))`, which must equal `max_samples` (4096 default) no matter how many observations arrive; tests/test_phase22_leaks_and_gate.py::test_metrics_sample_storage_is_bounded pushes 20,000 samples through Metrics(max_samples=50) and asserts 50 remain.

### Root causes

#### 1. Requirement 1: nearest-rank percentile, pinned, pure, empty-safe

**Why it hurts.** Without a percentile function the report falls back to `statistics.median` and `max(latencies)` (report.py:131-132 `_safe(...) or ...`), so the only tail number is the worst single request. The mean is a lie for latency because distributions are long-tailed: test_percentile_is_what_users_feel_not_the_mean is 90 requests at 0.05s and 10 at 5.0s. Mean = (4.5 + 50)/100 = 0.545s, which describes zero actual requests. p50 = 0.05s, p95 = 5.0s. Nearest-rank on 100 sorted samples: ceil(0.95*100) = rank 95; ranks 91-100 are all 5.0, so p95 = 5.0. If the scheme is not pinned, two engineers computing 'p95' with numpy's linear interpolation vs nearest-rank get different numbers on the same data and argue about a regression that is an interpolation artifact.

**Before**

```
origin/part4:docsbot/perf/metrics.py:65-72

    def percentile(samples: list[float], p: float) -> float:
        """Nearest-rank percentile of `samples` ...
        - Returns 0.0 for an empty list
        - Does not mutate the caller's list."""
        raise NotImplementedError("Phase 18: nearest-rank percentile.")
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:19-27

    def percentile(samples: list[float], p: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)          # sorted() returns a copy -> no mutation
        n = len(ordered)
        rank = math.ceil(p / 100.0 * n)    # 1-indexed rank
        rank = max(1, min(n, rank))        # clamp: p=0 -> first, p=100 -> last
        return ordered[rank - 1]
```

**Why the fix works.** Nearest-rank always returns a value that actually occurred (it indexes into the sorted list, never interpolates). `sorted()` allocates a new list so the caller's buffer is untouched (test_percentile_handles_unsorted_input_without_mutating_it). The clamp makes p=0 and tiny n safe: ceil(0*3)=0 -> clamped to 1 -> first element (test_percentile_clamps_low_p_to_first_element); p=100 -> rank n -> last. Empty -> 0.0 because 'a metric nobody recorded isn't an error'.

**Concept.** Report the distribution, not the average. The mean is dominated by the tail's magnitude; percentiles tell you what fraction of users saw what. Pin one percentile definition so numbers are comparable across runs.

**Tempting wrong fix.** `statistics.mean`, or `numpy.percentile` with default linear interpolation. Mean hides a 10% five-second tail inside 0.545s. Interpolated p95 over [1..100] is 95.05, so test_percentile_p95_over_100_samples (`== 95.0`) fails and p95 can report a latency no request ever had. Another shortcut is `samples.sort()` in place, which reorders the caller's ring buffer and destroys the 'drop oldest' ordering from Requirement 5.

#### 2. Requirement 2: Span.self_time_s = total minus children (self time localizes, total does not)

**Why it hurts.** In pipeline.py the spans nest: `ask` (254) contains `classify` (262), `retrieve` (272), `rerank` (201, called from inside ask), and `generate` (291). Sorted by TOTAL time, `ask` is always at the top -- it is the outermost span, so its total >= every child's by construction. That tells you nothing you did not know. test_stage_table_rolls_up_nested_spans encodes exactly this: `ask` total >= `retrieve` total, but `ask` self < `retrieve` self. test_span_duration_and_self_time: parent 0->10s containing child 1->7s has total 10s but self 4s. A parent with 900ms total and 5ms self is innocent -- something it called is guilty, and self time is the column that points at it.

**Before**

```
origin/part4:docsbot/perf/metrics.py:84-96

    @property
    def duration_s(self) -> float:
        raise NotImplementedError("Phase 18: total elapsed time.")

    @property
    def self_time_s(self) -> float:
        """Time in this span but NOT in any child span. ... A span whose self time
        is near zero is innocent"""
        raise NotImplementedError("Phase 18: duration minus children's duration.")
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:37-45

    @property
    def duration_s(self) -> float:
        if self.end is None:
            return 0.0
        return self.end - self.start

    @property
    def self_time_s(self) -> float:
        return max(0.0, self.duration_s - sum(c.duration_s for c in self.children))
```

**Why the fix works.** Self time is the time attributable to code in THIS span's own body. Because the stack is per-thread (Requirement 3), every child is fully contained in its parent's [start,end], so `duration - sum(children)` is exactly the time the parent spent not waiting on a child. `max(0.0, ...)` guards against clock jitter pushing the sum microscopically over the parent. An open span (end is None) reports 0.0 (test_open_span_has_zero_duration) rather than crashing. stage_table sums `self_s` per name and report.py sorts by it -- the top row is 'what do I fix first'.

**Concept.** Inclusive vs exclusive time (the same distinction every CPU profiler makes: 'total' vs 'self'). Inclusive time always blames the caller; exclusive time blames the code that actually burned the clock.

**Tempting wrong fix.** Only recording duration_s and sorting by total. You would 'discover' that `ask` is the bottleneck every time and never learn whether it is rerank's serial loop or generate's one big call. A second wrong fix is computing self time without a per-thread stack (Requirement 3): children from another thread get attached, the subtraction goes negative, and the table is noise.

#### 3. Requirement 3: per-thread span stack, exception-safe close, auto-sampled duration

**Why it hurts.** Three failure modes if done naively. (a) A single shared stack instead of `threading.local()`: with `--concurrency 8`, thread A opens `ask`, thread B opens `ask`, thread A's `retrieve` gets appended as a child of whichever `ask` is on top -- thread B's. B's self time collapses and A's inflates; test_spans_in_different_threads_do_not_nest asserts two concurrent spans produce 2 sibling roots with no children. (b) No try/finally: when the body raises (provider timeout, rate-limit error -- INC-003 territory) the span never gets `end`, never pops, and every later span in that thread becomes its child; test_span_closes_even_when_body_raises. (c) Not recording the duration as a sample means `percentile_of('ask', 95)` is empty (test_span_records_its_duration_as_a_sample) and you would need a parallel `observe()` at every call site.

**Before**

```
origin/part4:docsbot/perf/metrics.py:117-133

    @contextmanager
    def span(self, name: str) -> Iterator[Span]:
        """... Must close correctly even if the body raises ...
        Also record the span's duration as a sample under `name` ..."""
        raise NotImplementedError("Phase 18: implement the span context manager.")
        yield  # pragma: no cover

(and in __init__ at :111 the unused `self._local = threading.local()  # holds this thread's open-span stack`)
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:60-86

    def _stack(self) -> list[Span]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    @contextmanager
    def span(self, name: str) -> Iterator[Span]:
        stack = self._stack()
        sp = Span(name=name, start=time.perf_counter())
        stack.append(sp)
        try:
            yield sp
        finally:
            sp.end = time.perf_counter()
            if stack and stack[-1] is sp:
                stack.pop()
            elif sp in stack:
                stack.remove(sp)
            if stack:
                stack[-1].children.append(sp)
            else:
                with self._lock:
                    self._roots.append(sp)
            self.observe(name, sp.duration_s)
```

**Why the fix works.** `threading.local()` gives each thread its own stack, so nesting is a property of the call stack within one thread -- which is what 'parent' means. `try/finally` guarantees `end` is stamped and the span popped even when the body raises, and the exception still propagates (the test uses pytest.raises). The `stack[-1] is sp` / `remove` defensive pop survives a misbehaving inner span. Only the root append takes the process-wide lock; child attachment touches a thread-owned list. `self.observe(...)` is called AFTER the `with self._lock` block has exited -- `threading.Lock` is non-reentrant, so calling observe (which takes the lock) inside that block would deadlock. `time.perf_counter()` is monotonic and high-resolution, unlike `time.time()` which can jump under NTP adjustment.

**Concept.** Nesting is per call stack, and call stacks are per thread. Use thread-local storage for anything that models 'what am I currently inside'. Cleanup belongs in finally because the failure path is the one you most need to measure.

**Tempting wrong fix.** `self._stack: list[Span] = []` on the instance (shared). Passes every single-threaded test and the sibling test, then produces garbage under concurrency -- the exact mode INC-001 complains about (8 users -> 6x latency). Another half-fix: a plain `yield` without try/finally, which passes happy-path tests and leaks an open span the first time the provider throws.

#### 4. Requirement 4: lock-protected counters, copy-on-read snapshots (counters, samples, roots, percentile_of)

**Why it hurts.** `d[k] = d.get(k, 0) + n` is a read-modify-write; two threads can both read 5 and both write 6, losing an increment. test_counters_do_not_lose_increments_under_concurrency runs 8 threads x 500 increments and demands exactly 4000. Counters are the 'how many' half of diagnosis: 'p95 is 2s' is a symptom, `cache_miss == asks and cache_hit == 0` (pipeline.py:282-287) is the INC-002 cause. A counter that drifts under load 'will send you hunting for a bug that isn't there' (metrics.py:103-104). Returning the live dict lets a reader mutate state: test_counters_returns_a_copy sets snapshot['x']=999 and expects the real counter still to be 1.

**Before**

```
origin/part4:docsbot/perf/metrics.py:135-137 and :148-162

    def count(self, name: str, n: int = 1) -> None:
        """Increment a counter. Thread-safe."""
        raise NotImplementedError("Phase 18: increment a counter.")
    ...
    def counters(self) -> dict[str, int]:
        """Snapshot of every counter (a copy — callers must not mutate state)."""
        raise NotImplementedError("Phase 18: return a copy of the counters.")
    def samples(...): raise NotImplementedError(...)
    def percentile_of(...): raise NotImplementedError(...)
    def roots(...): raise NotImplementedError(...)
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:88-90 and :101-114

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def samples(self, name: str) -> list[float]:
        with self._lock:
            return list(self._samples.get(name, []))

    def percentile_of(self, name: str, p: float) -> float:
        return percentile(self.samples(name), p)

    def roots(self) -> list[Span]:
        with self._lock:
            return list(self._roots)
```

**Why the fix works.** One `threading.Lock` serializes every mutation and every snapshot; the critical section is a dict get/set, so contention is negligible. `dict(...)`/`list(...)` hand back copies, so a reader can iterate while writers continue without 'dictionary changed size during iteration', and nobody corrupts internal state by accident. `percentile_of` composes the two pure pieces. (Under CPython the GIL makes individual dict ops atomic, but `get` then `set` is two ops -- the lock is about the compound.)

**Concept.** Read-modify-write is never atomic without a lock. Expose snapshots, never internals: a metrics object is read by code you do not control.

**Tempting wrong fix.** Relying on the GIL ('dict assignment is atomic in CPython'). The individual `get` and `__setitem__` are atomic; the pair is not, so increments are lost intermittently under contention. `collections.Counter` does not help; `counter[k] += n` is still read-then-write.

#### 5. Requirement 5: bounded sample storage, drop oldest (the instrument must not be the leak)

**Why it hurts.** Every span exit calls `observe(name, duration)`. A naive append grows by one Python float per span per request. Rough arithmetic: a float object is 24 bytes plus an 8-byte list slot, ~32 bytes/sample. One ask produces 5 spans (ask, classify, retrieve, rerank, generate). At 50 asks/s that is 250 samples/s = 8 KB/s = ~29 MB/hour = ~170 MB over the 6-hour cycle INC-004 describes. Crucially the growth RATE is proportional to traffic, so memory fills fastest during peak hours and the pod dies during the load you most need observability for (INC-004:29-31 'It restarts most often during our busiest hours, because that's when it fills fastest'). Drop-NEWEST would be worse than useless: the percentile would freeze on the first 4096 samples and never reflect the regression deployed this morning.

**Before**

```
origin/part4:docsbot/perf/metrics.py:139-145

    def observe(self, name: str, value: float) -> None:
        """Record one histogram sample, keeping at most `max_samples` per name.
        When full, drop the OLDEST sample."""
        raise NotImplementedError("Phase 18: record a bounded sample.")

(DEFAULT_MAX_SAMPLES = 4096 at :62 is declared but nothing enforces it)
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:92-98

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self._samples.setdefault(name, [])
            bucket.append(value)
            if len(bucket) > self.max_samples:
                # Drop oldest; recent behaviour is what we're debugging.
                del bucket[: len(bucket) - self.max_samples]
```

**Why the fix works.** After append, if the bucket exceeds `max_samples`, `del bucket[:overflow]` removes from the front, so the retained window is always the most recent `max_samples` values. Memory per metric name is O(max_samples) regardless of uptime or traffic: 4096 floats x ~6 names is under 1 MB, fixed. test_observe_is_bounded_and_drops_oldest pushes 1000 into a cap of 100 and asserts min == 900, max == 999; test_default_sample_cap_is_finite checks the default. 4096 is 'enough for stable percentiles; bounded enough to run for a month' (metrics.py:60-61). Percentiles over the window remain meaningful (test_metrics_percentiles_still_work_when_bounded).

**Concept.** Anything that grows with traffic and is never trimmed is a leak, and observability code is not exempt. A sliding window answers 'what is happening now'; an ever-growing list answers 'what happened on average since boot'.

**Tempting wrong fix.** (1) `if len(bucket) < self.max_samples: bucket.append(value)` -- bounded, but drops the NEWEST sample and freezes p95 at boot-time behaviour; fails `max(samples) == 999`. (2) Bounding only at report time -- the list still grows between reports. (3) A per-process cap with no per-name cap: one chatty metric starves the others. A real limitation the reference solution leaves open: `_roots` (the span trees) is NOT bounded -- report.py:57/110 calls `metrics.reset()` before each measurement, which is why it is tolerable for a benchmark tool, but a long-lived server using this Metrics would still need to cap or drain `_roots`. A strong interview answer names this.

#### 6. Requirement 6: stage_table rollup walks the whole tree, keyed by name; report() is the contract

**Why it hurts.** Without the rollup you have thousands of individual span objects and no way to answer 'which STAGE is slow'. Rolling up only roots misses everything: in pipeline.py only `ask` (254) and `index` (110) are roots; `rerank`, `retrieve`, `classify`, `generate` are children and would vanish. The `count` column aggregates repeated stages: test_stage_table_counts_repeated_stages expects count==5 for 5 `rerank` spans. Note that in the real pipeline the `rerank` span wraps the whole candidate loop (pipeline.py:201-214), so span count is 1 per ask; the N+1 provider calls are visible as rerank's dominant `self_s` and as the provider's `calls_by_tag` (report.py:147), not as span count.

**Before**

```
origin/part4:docsbot/perf/metrics.py:165-185

    def stage_table(self) -> dict[str, dict]:
        """... "count" / "total_s" / "self_s"  <- where the time actually went / "p95_s"
        sort it by `self_s` descending and start at the top."""
        raise NotImplementedError("Phase 18: roll spans up into a stage table.")

    def report(self) -> dict:
        raise NotImplementedError("Phase 18: assemble the report.")
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:117-143

    def _walk(self) -> Iterator[Span]:
        def visit(sp: Span) -> Iterator[Span]:
            yield sp
            for child in sp.children:
                yield from visit(child)
        for root in self.roots():
            yield from visit(root)

    def stage_table(self) -> dict[str, dict]:
        buckets: dict[str, list[Span]] = {}
        for sp in self._walk():
            buckets.setdefault(sp.name, []).append(sp)
        table = {}
        for name, spans in buckets.items():
            durations = [s.duration_s for s in spans]
            table[name] = {
                "count": len(spans),
                "total_s": sum(durations),
                "self_s": sum(s.self_time_s for s in spans),
                "p95_s": percentile(durations, 95),
            }
        return table

    def report(self) -> dict:
        return {"stages": self.stage_table(), "counters": self.counters()}
```

**Why the fix works.** A recursive pre-order walk over the copied `roots()` snapshot visits every span at every depth, grouping by name. Per name it emits count, total_s (inclusive), self_s (exclusive -- the sort key), and p95_s over durations (test_stage_table_reports_p95_per_stage asserts `p95_s <= total_s`). `report()` bundles stages and counters into the dict report.py and budgets consume (test_report_has_stages_and_counters). Because it walks a snapshot, running the report while requests are in flight does not race with writers.

**Concept.** Aggregate by stage name, not by instance: a profile is a histogram over code locations, not a list of events. Ship the table in the shape the consumer reads, with the sort key (self time) in it.

**Tempting wrong fix.** Iterating `self._roots` only (no recursion), which reports just `ask`/`index` -- `set(table) == {'ask','retrieve','generate'}` fails. Or computing p95 over `self_time_s` instead of `duration_s`: the spec says p95 of this stage's durations.

#### 7. Requirement 7: reset() clears spans, counters, samples AND the calling thread's open-span stack

**Why it hurts.** report.py:57 and :110 call `m.reset()` before every measurement so the stage table reflects one run, not the warm-up plus every previous run. Without a reset, the 'before' and 'after' numbers in Phases 19-22 would be contaminated by each other and the baseline diff would be meaningless. test_reset_clears_everything asserts roots, counters, and samples are all empty afterwards.

**Before**

```
origin/part4:docsbot/perf/metrics.py:187-189

    def reset(self) -> None:
        """Drop all spans, counters, and samples."""
        raise NotImplementedError("Phase 18: clear all recorded state.")
```

**After**

```
origin/part4-solutions:docsbot/perf/metrics.py:145-150

    def reset(self) -> None:
        with self._lock:
            self._roots.clear()
            self._counters.clear()
            self._samples.clear()
        self._local.stack = []
```

**Why the fix works.** All three shared stores are cleared under the lock in one critical section; the calling thread's open-span stack is replaced so a stale span cannot adopt the next run's spans as children. Limitation worth knowing: `self._local.stack = []` only resets the thread that called reset(); other threads' stacks (normally empty between requests) are untouched.

**Concept.** A measurement must start from a known-empty state; a baseline you cannot reproduce is not a baseline.

**Tempting wrong fix.** Clearing `_roots`/`_counters`/`_samples` without the lock (races with an in-flight span's `_roots.append`), or forgetting the thread-local stack so a span left open by a prior exception becomes the parent of everything in the next run.

### Concepts to own

- Self (exclusive) time vs total (inclusive) time: total always blames the outermost caller; self time = duration minus children and points at the code that actually burned the clock. Sort the stage table by self_s.
- Why the mean lies and p95 tells the truth: 90 x 0.05s + 10 x 5.0s has mean 0.545s (nobody's experience), p50 0.05s, p95 5.0s. Nearest-rank percentile = sorted[ceil(p/100*n) - 1], clamped to [1,n], empty -> 0.0, never mutate input.
- Bounded storage as a correctness property: a metrics object that appends forever is a leak whose growth rate is proportional to traffic, so it OOMs fastest at peak load. Drop OLDEST so the window reflects now, not boot time. Know that _roots is still unbounded in the reference and why that is tolerable only for a benchmark that reset()s.
- Per-thread span nesting with threading.local() plus try/finally: nesting is a property of one thread's call stack; concurrent spans are siblings; exceptions are when you most need the timing. observe() must be called outside the lock because threading.Lock is non-reentrant.
- Thread-safe counters via a lock around read-modify-write, and copy-on-read snapshots so consumers cannot mutate internals; counters ('how many calls') turn a latency symptom into a call-count cause. In docsbot the per-tag call count comes from the provider's calls_by_tag, not from span count.
- The measurement loop: build the instrument first, reset() to a known state, save a baseline, and make every later claim a diff against it.

### Interview questions

**Q: Your stage table shows `ask` at 1.8s total and `rerank` at 1.6s total. Which do you fix, and how do you know?**

Neither number answers it; I look at self time. `ask` is the outer span so its total is by construction the sum of everything beneath it -- if its self time is ~5ms it is innocent, it is just waiting. `rerank` with 1.6s total and 1.6s self (no children of its own) is where the clock was actually burned. Then I cross-check with the call counters: in docsbot the rerank span wraps the whole candidate loop, so its span count is 1, but the provider's calls_by_tag shows ~8 rerank calls per ask -- a call-count problem disguised as latency. In code: `Span.self_time_s = max(0, duration - sum(child.duration))` and report.py sorting by `self_s` descending.

**Q: Why not just report average latency? It's one number and everyone understands it.**

Because latency is long-tailed and the mean is dominated by the tail's magnitude, not its frequency. Take 90 requests at 50ms and 10 at 5s: mean is 545ms, a latency no request had. p50 says half your users see 50ms; p95 says one in ten is staring at a spinner for 5 seconds. You make product decisions on 'what fraction of users see what', and only percentiles answer that. I pin nearest-rank -- `sorted[ceil(p/100*n)-1]` -- so p95 is always a latency that really occurred and two runs are comparable; interpolated schemes produce values no request had and differ between libraries.

**Q: Walk me through how your metrics object itself could cause an OOM, and what you did about it.**

Every span exit records a float sample. At ~32 bytes per sample, 5 spans per request and 50 req/s, that is ~29 MB/hour, ~170 MB over the 6-hour sawtooth in INC-004 -- and the rate scales with traffic, so it fills fastest at peak and kills the pod exactly when you need the data. The fix is a per-metric cap: after append, `if len(bucket) > max_samples: del bucket[:overflow]`, dropping the OLDEST so the retained window is the most recent 4096 and p95 reflects this morning's deploy. Memory per name becomes O(max_samples). I would add that the reference solution keeps `_roots` unbounded -- fine for a benchmark that calls reset() per run, but a long-lived server would need to cap or drain the span trees too.

**Q: Two request threads are both inside a `rerank` span at the same time. What does your span tree look like and why?**

Two sibling roots, neither a child of the other. Nesting means 'inside on the same call stack', and call stacks are per thread, so the open-span stack lives in `threading.local()`. With one shared stack, thread B's `retrieve` would attach to thread A's `ask`, A's self time would be inflated, B's would collapse, and every number from `--concurrency 8` would be nonsense -- precisely the mode INC-001 complains about. The only shared state is the roots list, counters, and samples, each behind one lock; child attachment touches a thread-owned list and needs no lock.

**Q: The provider throws mid-request. What happens to your span, and why does it matter?**

The span still closes: `span()` uses try/finally, so `end` is stamped with perf_counter, the span is popped off the thread's stack, attached to its parent or roots, and its duration is observed as a sample -- then the exception propagates unchanged. Failures are when you most want the timing (INC-003 is 90-second hangs); without finally the first exception leaves a zombie span open, every later span in that thread becomes its child, and the self-time math is corrupted for the rest of the process lifetime. One subtlety: observe() is called after the lock block has exited, because threading.Lock is non-reentrant and observe takes the same lock.

**Q: Why is `counters[name] = counters.get(name, 0) + n` not safe in CPython even with the GIL, and what's the test that proves it?**

The GIL makes each bytecode-level dict operation atomic, but get-then-set is two operations; a thread switch between them lets two threads read 5 and both write 6, losing an increment. The spec runs 8 threads x 500 increments and demands exactly 4000. A lock around the compound fixes it at negligible cost. I also return `dict(self._counters)` rather than the live dict so a reader can't mutate internal state or hit 'dict changed size during iteration' -- the test mutates the snapshot to 999 and checks the real counter is still 1.


---

## Phase 19 — Latency

**Incident:** INC-001 — "The bot got slow" (SEV3, opened by Dana Whitfield, Head of Support; SRE follow-up from Priya Raman)

> **One-line story:** Users waited ~2.5 s per answer, 8 concurrent users waited ~6x longer, and deploys flapped for 10 minutes; the stage table's self time showed a serial 8-call reranker eating ~75% of the request, a global lock around the whole ask() serialising every user, and a liveness probe that lazily embedded the corpus one chunk per call; batching rerank into one SCOREMANY call (11 -> 4 calls, ~175 ms saved), deleting the lock, making health() local (0 provider calls) and batching the index (50 -> 1 embeds) brought p95 from ~270 ms to ~95 ms against a 130 ms budget, concurrency to ~1-2x instead of 8x, and health to sub-millisecond — with every correctness guard still green.

### What was reported

Support: "DocsBot takes forever... about two and a half seconds before anything comes back. It used to feel instant." Answers are correct; users alt-tab away and forget they asked. Infra: instances "flapping" for ~10 minutes after every deploy — the LB health check (2 s timeout) times out on every fresh instance, the instance is pulled, the replacement does the same thing. SRE load test: 8 concurrent users ("nothing") pushed per-request latency from ~2.5 s to over 15 s — ~6x, "requests are queueing behind each other rather than running side by side."

### How you measure it

(1) `python -m docsbot.perf.report --asks 20`: read the `p95 latency` line against `settings.budget_ask_p95_s = 0.130` (config.py:124); read calls-by-tag — on origin/part4 `rerank` shows 8 chat calls per ask vs 1 each for classify/query_embed/answer (11 provider calls per ask); read the Phase 18 stage table sorted by self_s — `rerank` self time is ~200 ms of a ~270 ms ask (8 x chat_latency_s 25 ms, harness.py:139). (2) `--concurrency 8`: on part4 total wall for 8 asks ~= 8 x single-ask; after the fix ~= 1-2 x single-ask. Spec `test_concurrent_asks_do_not_serialize` asserts concurrent < 0.5 x serial; part4 gives ~1.0. (3) Health: `fresh_service.provider.call_count` after one `health()` — part4 makes ~50 embed calls (~750 ms harness units; corpus 30,961 bytes across 6 files, chunk step 800-100=700 chars); specs want 0 calls (`test_health_check_makes_no_provider_calls`) and 20 probes < 50 ms (`test_health_check_is_fast`). (4) `test_ask_does_not_stack_up_serial_provider_calls`: passes iff calls per ask <= 4 OR wall < 0.6 x summed provider duration; part4 is 11 calls with wall ~= provider time. Harness sleeps are ms-scale (harness.py:27-29): the ticket's 2.5 s is the same shape at ~10x. Note the harness's chat sleep depends on OUTPUT tokens only (25 ms + 10 ms per 1k output tokens; harness.py:226-227), so prompt-size reductions from INC-002 do not move Phase 19 numbers.

### Root causes

#### 1. N+1 serial rerank calls (the dominant self-time stage)

**Why it hurts.** Each `provider.chat()` is one round-trip that sleeps chat_latency_s = 25 ms plus 10 ms per 1k output tokens (harness.py:139-140, 226-227); a SCORE reply is one short number, so ~25 ms each. Eight candidates x 25 ms = 200 ms, stacked end to end because each call waits for the previous one. The cold ask totals ~270 ms of provider time (classify 25 + query embed 15 + rerank 200 + generate ~25-30), so rerank alone is ~75% of p95 — the solution's own docstring says '~80%'. At ticket scale that is ~1.9 s of the 2.5 s. The Phase 18 stage table shows it directly: `rerank` has by far the largest self_s while `ask` self time is tiny. Classic N+1: one call per item against an endpoint that accepts a batch.

**Before**

```
docsbot/service/pipeline.py:201-216 (origin/part4)

        with self.metrics.span("rerank"):
            scored: list[tuple[int, float]] = []
            for idx in candidate_ids:
                passage = self._chunks[idx].text
                reply = self.provider.chat(
                    f"SCORE:{question}|||{passage}",
                    model=settings.chat_model,
                    tag="rerank",
                )
                try:
                    score = float(reply.strip())
                except ValueError:
                    score = 0.0
                scored.append((idx, score))

Called from ask() (pipeline.py:277-278) with `candidates = fused[:settings.rerank_top_n]`; `rerank_top_n = 8` (config.py:62).
```

**After**

```
diff origin/part4..origin/part4-solutions, pipeline.py _rerank():

+        if not candidate_ids:
+            return []
+
         with self.metrics.span("rerank"):
-            scored: list[tuple[int, float]] = []
-            for idx in candidate_ids:
-                passage = self._chunks[idx].text
-                reply = self.provider.chat(
-                    f"SCORE:{question}|||{passage}",
+            passages = [self._chunks[i].text for i in candidate_ids]
+            reply = self.provider.chat(
+                "SCOREMANY:" + question + "|||" + "|||".join(passages),
+                model=settings.chat_model,
+                tag="rerank",
+            )
+            scores: list[float] = []
+            for raw in reply.split(","):
                 try:
-                    score = float(reply.strip())
+                    scores.append(float(raw.strip()))
                 except ValueError:
-                    score = 0.0
-                scored.append((idx, score))
+            scores += [0.0] * (len(candidate_ids) - len(scores))
+
+            scored = list(zip(candidate_ids, scores))
             scored.sort(key=lambda p: p[1], reverse=True)
```

**Why the fix works.** The harness's `_synthesize_reply` (harness.py:379-383) understands `SCOREMANY:<q>|||<p1>|||<p2>...` and returns comma-separated scores in one round-trip; `_overlap_score` (harness.py:347-348) is the same function for SCORE and SCOREMANY, so ordering is identical and `assert_ranked_by_relevance` still passes. Rerank drops from 8 calls/~200 ms to 1 call/~25 ms: ~175 ms saved per ask, calls per ask 11 -> 4. That alone moves p95 from ~270 ms to ~95 ms, under the 130 ms budget. The parse is defensive: scores are padded with 0.0 to len(candidate_ids) so a short reply cannot misalign candidates, and the empty-candidates early return avoids a pointless call.

**Concept.** Fewer calls beats faster calls. Round-trips, not bytes, are the unit of latency; when self time concentrates in one stage, look for a per-item loop against a batch-capable endpoint. Batching fixes latency AND cost AND rate-limit headroom; parallelising only fixes latency.

**Tempting wrong fix.** Wrap the loop in a ThreadPoolExecutor — config dangles `rerank_concurrency = 8` (config.py:111-113) as bait, and the solution file even imports ThreadPoolExecutor (line 15) without using it. The Phase 19 spec explicitly accepts that (its docstring says parallelising or batching both pass), but it still makes 8 calls, so it fails Phase 20's `budget_ask_provider_calls = 4` (config.py:126), costs the same money, and burns 8x the rate-limit quota. Equally wrong: cut `rerank_top_n` to 1 or delete reranking — faster by making the product worse, and it trips `test_reranking_still_orders_by_relevance` / `test_retrieval_still_finds_the_right_document`.

#### 2. Global lock turns the service into a queue (8 users -> ~8x latency)

**Why it hurts.** Every `ask()` holds one process-wide mutex for its whole duration, including all the time spent sleeping on the network. With 8 concurrent callers, request k waits behind the k-1 ahead of it: latencies are 1x, 2x, ..., 8x the single-ask time — mean 4.5x, tail 8x — matching the SRE's 'close to fully serial, ~6x' (2.5 s -> 15 s+). Concurrency buys nothing because the contended resource is not CPU; it is a lock wrapped around idle waiting. The comment's premise is false: the FakeProvider docstring (harness.py:128-132) says 'Thread-safe. Note that the simulated network delay happens *outside* the lock — otherwise this class would serialize your concurrency.' 'Cheap insurance' against a non-existent hazard cost an 8x tail.

**Before**

```
docsbot/service/pipeline.py:33-35 and :253-255 (origin/part4)

# The provider client isn't documented as thread-safe, so we serialize access
# across the whole request to be safe. Cheap insurance.
_ASK_LOCK = threading.Lock()
...
    def ask(self, question: str, *, history=None) -> Answer:
        started = time.perf_counter()

        with _ASK_LOCK:
            with self.metrics.span("ask"):
                self._ensure_indexed()
                ...   # classify, embed, rerank, generate all run inside the lock
```

**After**

```
diff pipeline.py:

-import threading
...
-# The provider client isn't documented as thread-safe, so we serialize access
-# across the whole request to be safe. Cheap insurance.
-_ASK_LOCK = threading.Lock()
...
-        with _ASK_LOCK:
-            with self.metrics.span("ask"):
-                self._ensure_indexed()
+        # INC-001: no global lock. The provider client is thread-safe, and
+        # serialising every request behind one mutex meant eight concurrent
+        # users waited eight times as long as one user.
+        with self.metrics.span("ask"):
+            self._ensure_indexed()
```

**Why the fix works.** Deleting the lock lets 8 threads sleep concurrently on their provider calls; wall clock for 8 asks drops from ~8x single to roughly 1x single plus GIL contention on the pure-Python dot products and TF-IDF — which is why the ticket's DoD says 'not more than ~2x' and the spec asserts concurrent < 0.5 x serial. Nothing needed the lock: `_chunks`/`_vectors` are read-only after indexing, and `AnswerCache` and `SessionStore` carry their own locks for the only mutable shared state. (Residual caveat: `_ensure_indexed()` is still lazy and unlocked in the solution, so two first-requests racing on a cold instance could both index; the spec does not test that.)

**Concept.** A lock held across I/O converts concurrency into a queue. Lock the data, not the request; lock only mutable shared state, and never hold a lock while waiting on the network. Before adding 'defensive' serialisation, verify the thing you are protecting is actually unsafe — and measure what the insurance costs under load.

**Tempting wrong fix.** Make the lock 'finer-grained' by wrapping only the `provider.chat()`/`provider.embed()` calls — that still serialises ~100% of wall time, since the provider calls ARE the wall time. A `Semaphore(8)` or per-question lock just moves where the queue forms. More worker processes (gunicorn -w 8) hides the lock at 8x the memory and breaks as soon as load exceeds worker count.

#### 3. Liveness probe triggers cold-start indexing (deploy flapping)

**Why it hurts.** A fresh instance has `_indexed = False`. The LB's first probe calls `health()` -> `_ensure_indexed()` -> `index(load_corpus())`, which on origin/part4 embeds one chunk per call: 6 handbook files, 30,961 bytes, 700-char step (chunk_size 800 - overlap 100) ~= 50 chunks x 15 ms embed_latency_s = ~750 ms in harness units — seconds at production scale, past the LB's 2 s timeout. The probe times out, the LB pulls the instance, the replacement is also cold and repeats it. That loop is the '~10 minutes of flapping after each deploy'. The expensive work hid behind the one endpoint that must be trivially cheap and is hit most often.

**Before**

```
docsbot/service/pipeline.py:133-143 (origin/part4)

    def _ensure_indexed(self) -> None:
        if not self._indexed:
            self.index(load_corpus())

    def health(self) -> dict:
        """Liveness probe. Hit by the load balancer every few seconds."""
        self._ensure_indexed()
        return {"status": "ok", "chunks": len(self._chunks)}
```

**After**

```
diff pipeline.py, health():

     def health(self) -> dict:
-        """Liveness probe. Hit by the load balancer every few seconds."""
-        self._ensure_indexed()
-        return {"status": "ok", "chunks": len(self._chunks)}
+        """Liveness probe: cheap, local, and free.
+
+        INC-001: this used to lazily index the corpus, so the first probe after
+        a deploy triggered a full embed inside the load balancer's timeout —
+        the probe timed out, the LB pulled the instance, and the replacement
+        did exactly the same thing. A health check must never be the thing that
+        does the expensive work.
+        """
+        return {"status": "ok", "chunks": len(self._chunks),
+                "indexed": self._indexed}

(`_ensure_indexed()` is unchanged and still called from ask() — solutions pipeline.py:135, 271 — so the first real request, not the probe, pays for lazy indexing.)
```

**Why the fix works.** `health()` now touches only local memory — status, chunk count, and an `indexed` flag so readiness can be distinguished from liveness — and makes zero provider calls. 20 probes finish well under 50 ms (`test_health_check_is_fast`), and `test_health_check_makes_no_provider_calls` asserts call_count == 0 on an unindexed `fresh_service`. Indexing moves to the first real ask (a fuller production fix would warm it at startup in the background). The cold start itself also shrinks ~50x via batched indexing (next cause), but the probe must be cheap regardless.

**Concept.** A liveness probe answers 'is the process alive', not 'is the cache warm', and must never trigger cold-start or any expensive work. Separate liveness from readiness, and never put lazy initialisation behind an endpoint an automated system calls on a timeout — the timeout becomes a restart loop.

**Tempting wrong fix.** Ask infra to raise the LB timeout to 10 s. That hides the symptom, keeps the probe the most expensive endpoint, leaves the first user on each new instance eating the cold start, and delays detection of genuinely dead instances. Another half-fix: keep `_ensure_indexed()` in health but catch/timeout it — the work still runs synchronously inside the probe.

#### 4. One-embed-per-chunk indexing (cold-start amplifier; tagged INC-002 in the diff but it is what makes the cold instance fast)

**Why it hurts.** `embed()` costs one round-trip per CALL, not per text, up to MAX_BATCH = 100 (harness.py:52, 189-202: one `_sleep(embed_latency_s)` per call). Embedding ~50 chunks one at a time is 50 serial round-trips x 15 ms = ~750 ms; the same 50 texts in one call is 15 ms. On origin/part4 this is exactly the work the health probe triggered, so it is the multiplier behind the flapping, and it is what the first real request pays after every deploy ('runs on every deploy, several times a day').

**Before**

```
docsbot/service/pipeline.py:117-128 (origin/part4)

            # Re-embed the whole corpus so the index is definitely consistent
            # with what's on disk.
            self._chunks = []
            self._vectors = []

            for chunk in chunks:
                vec = self.provider.embed([chunk.text],
                                          task_type="RETRIEVAL_DOCUMENT",
                                          tag="index")[0]
                self._chunks.append(chunk)
                self._vectors.append(vec)
                self.metrics.count("chunks_embedded")
```

**After**

```
diff pipeline.py, index():

+            hashes = [_content_hash(c.text) for c in chunks]
+            todo = [(h, c) for h, c in zip(hashes, chunks)
+                    if h not in self._vector_by_hash]
+            unique: dict[str, str] = {}
+            for h, c in todo:
+                unique.setdefault(h, c.text)
+
+            pending = list(unique.items())
+            for start in range(0, len(pending), MAX_BATCH):
+                page = pending[start:start + MAX_BATCH]
+                vectors = self.provider.embed([text for _, text in page],
+                                              task_type="RETRIEVAL_DOCUMENT",
+                                              tag="index")
+                for (h, _), vec in zip(page, vectors):
+                    self._vector_by_hash[h] = vec
+                self.metrics.count("chunks_embedded", len(page))
+
+            self._chunks = chunks
+            self._vectors = [self._vector_by_hash[h] for h in hashes]

(plus `from ..perf.harness import MAX_BATCH, FakeProvider` and the `_vector_by_hash` dict in __init__)
```

**Why the fix works.** Texts are paged into batches of MAX_BATCH, so a cold index is ceil(50/100) = 1 embed call (~15 ms) instead of ~50 (~750 ms) — well inside any LB timeout even if someone later reintroduces warm-up into the probe. The content-hash cache (`_vector_by_hash`) additionally makes re-indexing unchanged docs zero provider calls — Phase 20's concern. The mentor labels this hunk INC-002, but it is the second half of why a cold instance now comes up fast.

**Concept.** Know the latency/pricing unit of the endpoint you call. When the unit is 'per call' and the API accepts a list, a per-item loop is an N+1 bug whether it is reranking or embedding — the same principle as the rerank fix, applied to startup.

**Tempting wrong fix.** Parallelise the 50 single-text embeds with a thread pool: wall time drops but it is still 50 calls, 50x the rate-limit pressure, and it fails Phase 20's index-call budget. Or persist vectors to disk and skip embedding — reasonable in production, but it does not fix the probe and it is not what the spec measures.

#### 5. Adjacent hunks in the same diff: small or zero latency effect — know which is which

**Why it hurts.** Mostly it doesn't, and being able to say so is the skill. (a) `build_client()` in the harness (harness.py:179-184) only increments a counter under a lock: ~0 ms. It matters for Phase 22's `client_builds` check (a real SDK would open a new connection pool per request), not for INC-001. (b) The 'embed once' comment overstates history: on origin/part4 `query_embed` fires once per ask (only in `_vector_search`; `_fuse` never embeds), so `test_query_is_embedded_exactly_once_per_ask` already passes on the broken code. (c) Classify on `cheap_model` IS a small real latency win: CHEAP_LATENCY_FACTOR = 0.4 (harness.py:62) takes classify from ~25 ms to ~10 ms — ~15 ms of the ~270 ms ask, tagged INC-002. (d) Cache-first ordering only helps repeat questions: on part4 a cache HIT still paid classify + embed + 8 reranks (~240 ms); the Phase 19 p95 test clears the cache before every ask, so this does not move the spec's headline number. (e) The prompt-shrinking hunks (chunk not full doc, top_k not all candidates, history window) cut INPUT tokens; the harness's chat sleep depends only on output tokens, so zero Phase 19 effect.

**Before**

```
docsbot/service/pipeline.py (origin/part4)

:257-258
                # Fresh client per request keeps request state cleanly isolated.
                self.provider.build_client()

:148-150 — the query embed already happens exactly ONCE per ask:
    def _vector_search(self, question: str, limit: int):
        qvec = self.provider.embed([question], task_type="RETRIEVAL_QUERY",
                                   tag="query_embed")[0]

:262-267 — classify on the flagship model:
                with self.metrics.span("classify"):
                    self.provider.chat(
                        f"CLASSIFY:{question}",
                        model=settings.chat_model,
                        tag="classify",
                    )

:272-287 — cache consulted only AFTER classify, retrieve and rerank have run.
```

**After**

```
diff pipeline.py:

-                # Fresh client per request keeps request state cleanly isolated.
-                self.provider.build_client()

+                # INC-001: embed the query ONCE and reuse the vector. It used to
+                # be embedded separately for vector search and for fusion.
+                qvec = self.provider.embed([question], task_type="RETRIEVAL_QUERY",
+                                           tag="query_embed")[0]
+                vector_hits = self._vector_search(qvec, settings.rerank_top_n * 2)

+            # INC-002: a yes/no guard does not need the flagship model.
+            with self.metrics.span("classify"):
+                self.provider.chat(f"CLASSIFY:{question}",
+                                   model=settings.cheap_model, tag="classify")

+            # INC-002: check the cache FIRST. ...
+            cache_key = AnswerCache.key(...)
+            if not history:
+                cached = self.cache.get(cache_key)
+                if cached is not None:
+                    ...return Answer(..., cached=True)
```

**Why the fix works.** Hoisting the embed into `ask()` and passing `qvec` down makes the 'embed once' invariant structural. Dropping per-request `build_client()` is hygiene that pays in Phase 22 and in any real SDK (TLS handshake, connection pool). Cheap-model classify buys ~15 ms and most of its value in Phase 20 dollars. None of these is why p95 fell from ~270 ms to ~95 ms; the three causes above are.

**Concept.** Attribute savings to the hunk that actually produced them. When a diff's comments claim more than the baseline supports, trust the baseline measurement and `provider.calls_of(tag=...)` over the comment. Not every line in a fix commit is a fix for this incident.

**Tempting wrong fix.** Listing 'removed build_client per request' or 'stopped embedding the query twice' in the INC-001 postmortem as wins. An interviewer who asks 'how many ms did that buy?' gets 'zero', which undermines the rest of the story.

### Concepts to own

- Self time vs total time: sort the stage table by self_s; the stage with the biggest self time is where wall clock is actually spent (here: rerank, ~200 of ~270 ms). Total time on the parent 'ask' span tells you nothing about which child to fix.
- Round-trips are the unit of latency. N+1 calls against a batch-capable endpoint (rerank via SCORE x8 vs SCOREMANY x1; embed one chunk per call vs one page per call) stack sleeps end to end. Batching removes the calls; parallelising only hides them — batching helps latency, cost, and rate-limit headroom simultaneously.
- A lock held across I/O turns a concurrent service into a queue: 8 users behind one mutex means latencies of 1x..8x (mean 4.5x, worst 8x — the ticket's ~6x). Lock mutable shared data, never the whole request, and verify the thing you are 'protecting' is actually unsafe before paying for the insurance.
- Liveness vs readiness: a health probe must be local, free, and never trigger lazy/cold-start work, because an LB with a timeout turns any slow probe into a restart loop (deploy flapping). Report 'indexed' separately if ops needs it.
- p95, not the mean: the mean hides the users who are suffering. Measure before and after, save the baseline, and state each fix as 'bought X ms'. A fix without a before/after number didn't happen.
- Correctness guards define the floor: the fastest DocsBot returns '' instantly. Rerank batching is acceptable precisely because SCOREMANY uses the same scoring function as SCORE, so ordering (assert_ranked_by_relevance) and grounding (assert_grounded) are unchanged.

### Interview questions

**Q: Support says 'the bot got slow, about 2.5 seconds.' Walk me through how you found where the time actually went.**

First I wrote down a baseline: `report --asks 20` gave p95 ~270 ms in harness units (the ticket's 2.5 s at production scale) and 11 provider calls per ask. Then I read the Phase 18 stage table sorted by self time, not total time — total time on 'ask' is useless because it contains everything. Rerank had ~200 ms of self time, ~75% of the request, and the calls-by-tag view showed why: 8 'rerank' calls per ask, one per candidate, each a 25 ms round-trip run end to end. Classify, query embed, and generate were ~25, 15, and ~25-30 ms. So the ranked list was: serial rerank (~200 ms), then everything else combined (~70 ms). Instinct says 'the LLM answer call is slow'; the instrument said the reranker loop was.

**Q: You could have parallelised the eight rerank calls and passed the latency test. Why batch instead?**

Because parallelising changes latency and nothing else. Eight concurrent calls still cost eight calls: same dollars, eight times the rate-limit quota, eight chances to hit a 429 or a hang. The harness — like real rerank endpoints — accepts a batch (SCOREMANY) and scores all passages in one round-trip with the same scoring function, so ordering is identical and the correctness guard still passes. Batching took rerank from 8 calls/200 ms to 1 call/25 ms, cut per-ask calls from 11 to 4, and that same change is what makes Phase 20's 4-call budget and Phase 21's retry story sane. Fewer calls beats faster calls. The `rerank_concurrency = 8` setting in config was bait; the Phase 19 spec even says parallelising passes — Phase 20 is where it bites.

**Q: Eight concurrent users made latency 6x worse. What was the mechanism, and what did you actually need to protect?**

A module-level `_ASK_LOCK` wrapped the entire `ask()` body, including all the time spent sleeping on provider calls. With 8 concurrent requests, request k waits for the k-1 ahead of it, so latencies are 1x through 8x the single-ask time — mean 4.5x, tail 8x, which is the SRE's 'close to fully serial, ~6x.' The comment justified it as 'the provider isn't documented as thread-safe,' but the provider's own docstring says it is thread-safe and deliberately sleeps outside its lock. Nothing needed it: chunk and vector lists are read-only after indexing, and the cache and session store have their own locks. I deleted it; 8 asks now take about one ask's wall time plus GIL contention on the pure-Python math, inside the 'no more than ~2x' target. Rule: lock data, not requests, and never hold a lock across I/O.

**Q: Infra says instances flap for ten minutes after every deploy. Why would that be your bug and not theirs?**

Because `/health` was secretly the most expensive endpoint we owned. On a fresh instance `_indexed` is False, and `health()` called `_ensure_indexed()`, which embedded the entire corpus — about 50 chunks, one embed call each, ~50 serial round-trips — inside the load balancer's 2 s probe timeout. The probe timed out, the LB pulled the instance, the replacement was also cold and did the same thing, and it only settled when one happened to finish first. Two fixes: health now returns local state only (status, chunk count, an 'indexed' flag) and makes zero provider calls — 20 probes in under 50 ms — and indexing was batched so the cold start itself is 1 embed call instead of 50. Raising the LB timeout would have hidden it and left the first real user paying the cold start.

**Q: the mentor's diff also removes `build_client()` per request, refactors the query embed, and moves classify to the cheap model. How many milliseconds did those buy?**

Zero, zero, and about 15. In the harness `build_client()` just increments a counter; it matters for Phase 22's `client_builds` check because a real SDK would open a new connection pool per request, but it's not latency here. The 'embed the query once' comment overstates history — on the broken branch the query was already embedded exactly once, inside `_vector_search`, and that spec passed before the fix; the refactor makes the invariant structural but doesn't move p95. Classify on the cheap model is real but small: the harness runs cheap models at 0.4x latency, so ~25 ms becomes ~10 ms — tagged INC-002 because its main value is dollars. Attributing savings to hunks that didn't produce them is how postmortems become folklore.

**Q: Give me the one-line-per-cause summary with the numbers, the way the ticket's definition of done asks for it.**

(1) Serial N+1 rerank: 8 x 25 ms round-trips -> one batched SCOREMANY call; bought ~175 ms of a ~270 ms ask (~1.75 s of the 2.5 s), calls per ask 11 -> 4, p95 now ~95 ms vs a 130 ms budget. (2) Global `_ASK_LOCK` around the whole request: 8 concurrent asks went from ~8x single latency (2.1 s tail) to roughly 1-2x by deleting a lock that protected nothing. (3) `/health` lazily indexed the corpus (~50 serial embeds, ~750 ms harness / seconds in prod) inside a 2 s LB timeout -> health is now local and free, 0 provider calls, sub-millisecond; plus index batching turned the cold start from ~50 calls into 1. Correctness guards (grounded, ranked, right document) stayed green throughout.


---

## Phase 20 — Cost (closes INC-002)

**Incident:** INC-002 — "Why is this line item eleven thousand dollars" (VP Finance, SEV2)

> **One-line story:** Finance saw a 12x bill on 3x traffic with a 0% cache hit rate; the cause was a pipeline that paid full price for repeat questions (inverted TTL check plus a cache consulted after 10 of 11 calls), re-embedded the whole corpus one chunk per call on every deploy and first health probe, made 8 flagship rerank calls where one batched call would do, grounded on whole documents for all 8 candidates instead of 4 retrieved chunks, resent the entire transcript every turn (quadratic), and ran an unused yes/no guard on the flagship model; fixing those (`>=` in cache.py, cache-first `ask()`, content-hashed batched indexing, SCOREMANY rerank, `chunk.text` for `ranked[:top_k]`, `history[-3:]`, `cheap_model` for classify) took an ask from 11 calls and ~5.5–9k input tokens to 4 calls and ~2.5k tokens on a miss and zero on a hit, made an unchanged re-index cost zero calls, and flattened conversation cost after turn 4 — because removing calls, unlike parallelising them, cuts latency, cost and rate-limit burn at once.

### What was reported

Marcus (VP Finance): invoice is $11,400 against a $900 budget. Traffic is up ~3x since launch but the bill is up ~12x, and "those numbers don't go together." From the provider dashboard he sees (1) an enormous number of tokens per question with no feature shipped to explain it, and (2) a huge spike in call volume on every deploy, and "we deploy several times a day now." Support says the same FAQ ("how do I export settlement reports") is asked hundreds of times a week, so why are we paying full price each time? Sam (Eng Manager) adds: long conversations are disproportionately expensive — a 10-turn chat costs far more than 10x a 1-turn chat, and "I don't think that's just how chat works." Known facts in the ticket (incidents/INC-002-the-bill.md): cost per ask ~6x a lean implementation; the cache is wired in but has a measured 0% hit rate; every deploy re-indexes the corpus even with no doc change; turn 10 costs several times turn 1. The ticket says there are "at least five distinct" wastes and asks you to rank them by dollars before fixing.

### How you measure it

Instrument: `python -m docsbot.perf.report --index --turns 16` (flags verified in report.py: --asks, --concurrency, --turns, --index). FakeProvider in docsbot/perf/harness.py prices every call: count_tokens = ~4 chars/token; settings.usd_per_1m_input = 0.075, usd_per_1m_output = 0.30; CHEAP_COST_FACTOR = 0.2 and CHEAP_LATENCY_FACTOR = 0.4 for settings.cheap_model; MAX_BATCH = 100 texts per embed call. Numbers to look at, BEFORE -> AFTER (static estimate from the code; the orchestrator's run gives the measured figure): (a) provider calls per ask, `provider.summary()['calls_by_tag']` — before: classify 1 + query_embed 1 + rerank 8 + answer 1 = 11; after: 1+1+1+1 = 4, exactly `settings.budget_ask_provider_calls = 4`. (b) `provider.input_tokens` per ask — before roughly 5,500–9,300 (8 SCORE prompts ≈1,700 + 3–6 whole ~1,250-token handbook docs in the answer prompt); after ≈2,500–2,600 (one SCOREMANY ≈1,600 + top_k=4 chunks x ~200 = 800 + system/question/classify), budget `budget_ask_input_tokens = 3200`. (c) `provider.usd` per ask vs `budget_ask_usd = $0.00034` (after ≈ $0.0002–0.0003 on a miss, $0 on a hit). (d) `service.cache.stats()` / `cache.hit_rate` — 0.0 before (report.py cycles through a QUESTIONS list so repeats exist), >0 after. (e) "RE-index of the same, unchanged corpus" — before ~44 embed calls (6 handbook files of ~5,000 chars, one 6,056; at chunk_size 800 / overlap 100 that is 7–9 chunks each); after 0 calls; and the FIRST index goes from ~44 calls to 1 batched call (44 <= MAX_BATCH). (f) CONVERSATION table: input_tokens per turn climbing by ~h per turn (h ≈ 250–260 tokens for a ~1,000-char harness answer plus the question) -> flat from turn 4 on. Spec: `pytest -m phase20` (tests/test_phase20_cost.py); the Phase 22 gate reads the same numbers under the names ask.provider_calls, ask.input_tokens, ask.usd.

### Root causes

#### 1. Cache that (effectively) never hits: inverted TTL comparison

**Why it hurts.** The comment says 'older than we allow' but the test is `age < ttl`, i.e. YOUNGER than the TTL. The logic is exactly inverted: any entry younger than 300 s is popped and counted as a miss; only an entry that has sat untouched for >= 300 s would be served (and it would be the stale one). Because the caller re-puts a fresh entry after every miss, the clock resets on every ask, so for any question asked more often than once per TTL — which is every question that matters, like the FAQ asked hundreds of times a week — the hit rate is exactly 0%. The product is still correct, so nothing alarms. N asks of 'export settlement reports' cost N full pipelines (11 calls, ~5.5k–9k input tokens each) instead of 1. It also explains why cost per request rose as traffic rose: a working cache makes the marginal cost of repeat traffic ~$0, so cost should grow SUB-linearly with users; with a dead cache it grows linearly at full price and the projected economies of scale never arrive.

**Before**

```
docsbot/service/cache.py:51-58 (origin/part4)
    age = now - entry.stored_at
    if age < self.ttl_s:
        # Entry is older than we allow -> treat as a miss and drop it.
        self._data.pop(key, None)
        self.misses += 1
        return None
    self.hits += 1
    return entry.value
```

**After**

```
docsbot/service/cache.py (diff, one character)
-            if age < self.ttl_s:
+            if age >= self.ttl_s:
                 # Entry is older than we allow -> treat as a miss and drop it.
```

**Why the fix works.** With `>=` the stale branch fires only for entries at or past the TTL, so a second ask within 300 s (`settings.answer_cache_ttl_s`) returns the stored Answer. `test_cache_hit_rate_is_reported` depends on this line alone; `test_repeated_question_costs_nothing_the_second_time` and `test_cache_is_consulted_before_the_expensive_work` only pass together with root cause #2, because a hit that still costs 10 calls is not 'nothing'.

**Concept.** A cache that never hits is invisible. Correctness tests cannot catch it; only a hit-rate metric can. Instrument it before you trust it, and treat 0% as an alarm, not a baseline.

**Tempting wrong fix.** Bump the TTL, or 'the cache must be too small — raise max_entries'. Neither changes the comparison, so hit rate stays 0% (a longer TTL actually makes it worse: entries must sit idle even longer before the inverted test lets one through). Another tempting half-fix: put `history` into the cache key so chat turns can be cached — that key never repeats, so it hits 0% by construction in chat mode; the reference instead bypasses the cache entirely when history is present.

#### 2. Cache consulted AFTER the work it was meant to avoid

**Why it hurts.** Even with the TTL bug fixed, `cache.get` sat at line 280, after classify (1 call), the query embed (1 call) and the serial reranker (8 calls) — 10 of the 11 calls. (The solutions-branch comment says 'nine'; count the part4 code and it is ten.) A 'hit' would have skipped only the final `answer` call, and the rerank calls alone are ~1,700 input tokens. So the cache could at best save one call out of eleven — 'a cache that saves the cheapest part of the request and pays for the rest.' Note the key was computed at line 269, BEFORE retrieval: nothing about the lookup needed the retrieval results, it was simply placed late. The old hit path also mutated the cached object in place (`answer.cached = True`, `answer.latency_s = ...`), so the stored entry's fields drifted with every hit.

**Before**

```
docsbot/service/pipeline.py:262-287 (origin/part4)
    with self.metrics.span("classify"):
        self.provider.chat(f"CLASSIFY:{question}", model=settings.chat_model, tag="classify")   # 1 call
    cache_key = AnswerCache.key(question, model=settings.chat_model, top_k=settings.top_k)   # line 269: key already known here
    with self.metrics.span("retrieve"):
        vector_hits = self._vector_search(question, settings.rerank_top_n * 2)   # 1 embed call
        ...
    candidates = fused[:settings.rerank_top_n]
    ranked = self._rerank(question, candidates)                                   # 8 chat calls
    cached = self.cache.get(cache_key)        # <-- line 280, after 10 provider calls
    if cached is not None:
        self.metrics.count("cache_hit")
        answer = cached
        answer.cached = True                  # mutates the shared cached object
        answer.latency_s = time.perf_counter() - started
        return answer
```

**After**

```
docsbot/service/pipeline.py (diff)
+            # INC-002: check the cache FIRST. It used to be consulted after
+            # classification, retrieval, and reranking had already run ...
+            cache_key = AnswerCache.key(question, model=settings.chat_model,
+                                        top_k=settings.top_k)
+            if not history:
+                cached = self.cache.get(cache_key)
+                if cached is not None:
+                    self.metrics.count("cache_hit")
+                    return Answer(text=cached.text, citations=cached.citations,
+                                  latency_s=time.perf_counter() - started,
+                                  cached=True)
+            self.metrics.count("cache_miss")
+            # only now: classify, embed, retrieve, rerank, generate
 ...
+            if not history:
                 self.cache.put(cache_key, answer)
```

**Why the fix works.** The key is computable from the question and settings alone, so the lookup moves to the top of `ask()`; on a hit the function returns before any provider call: 0 calls, 0 tokens, $0 (`test_cache_is_consulted_before_the_expensive_work` resets the provider and asserts call_count == 0). The `if not history:` guard on both get and put keeps conversational answers out of a question-keyed cache — a history-dependent answer must not be served to a different conversation, and a history-free cached answer must not short-circuit a chat turn. Returning a fresh `Answer(...)` instead of the stored object removes the aliasing.

**Concept.** A cache only saves what comes after it. Place the lookup at the earliest point where the key is known, and key it on everything the answer depends on (question, model, top_k) — and nothing it doesn't.

**Tempting wrong fix.** Fix only the `<`/`>=` bug and leave the lookup where it is: hit rate goes to a healthy number, dashboards look great, and each 'hit' still costs 10 provider calls — ~90% of the round-trips and maybe 25–30% of the tokens. The metric would say the cache works; the bill would barely move.

#### 3. Full re-embed of every chunk, one call per chunk, on every deploy (and on the first /health probe)

**Why it hurts.** Two independent wastes. (1) N+1 on a batch endpoint: `embed([chunk.text])` is called once per chunk. The handbook is 6 files of ~5,000 chars (one is 6,056); at chunk_size 800 / overlap 100 (700-char stride) that is 7–9 chunks per doc, ~44 chunks, so ~44 round-trips where the endpoint accepts up to MAX_BATCH=100 texts in ONE call (harness.py: 'ONE round-trip regardless of batch size'). (2) Everything is re-embedded every time `index()` runs: `_chunks`/`_vectors` are cleared and rebuilt, ~44 calls and ~8,000 input tokens per process start, per pod — and the first load-balancer /health probe triggers it lazily too. The docs change weekly; deploys happen several times a day. That is Marcus's 'huge spike in call volume every time we deploy', and it scales with deploy count and pod count, not with users — part of why the bill grew 4x faster than traffic.

**Before**

```
docsbot/service/pipeline.py:117-128 (origin/part4)
    # Re-embed the whole corpus so the index is definitely consistent
    # with what's on disk.
    self._chunks = []
    self._vectors = []
    for chunk in chunks:
        vec = self.provider.embed([chunk.text],
                                  task_type="RETRIEVAL_DOCUMENT",
                                  tag="index")[0]
        self._chunks.append(chunk)
        self._vectors.append(vec)
        self.metrics.count("chunks_embedded")

pipeline.py:140-143:
    def health(self) -> dict:
        self._ensure_indexed()          # LB probe triggers the full index
        return {"status": "ok", "chunks": len(self._chunks)}
```

**After**

```
docsbot/service/pipeline.py (diff)
+from ..perf.harness import MAX_BATCH, FakeProvider
+def _content_hash(text: str) -> str:
+    return hashlib.sha256(text.encode("utf-8")).hexdigest()
 ...
+        self._vector_by_hash: dict[str, list[float]] = {}
 ...
+            hashes = [_content_hash(c.text) for c in chunks]
+            todo = [(h, c) for h, c in zip(hashes, chunks)
+                    if h not in self._vector_by_hash]
+            unique: dict[str, str] = {}
+            for h, c in todo:
+                unique.setdefault(h, c.text)
+            pending = list(unique.items())
+            for start in range(0, len(pending), MAX_BATCH):
+                page = pending[start:start + MAX_BATCH]
+                vectors = self.provider.embed([text for _, text in page],
+                                              task_type="RETRIEVAL_DOCUMENT",
+                                              tag="index")
+                for (h, _), vec in zip(page, vectors):
+                    self._vector_by_hash[h] = vec
+                self.metrics.count("chunks_embedded", len(page))
+            self._chunks = chunks
+            self._vectors = [self._vector_by_hash[h] for h in hashes]

     def health(self) -> dict:
-        self._ensure_indexed()
-        return {"status": "ok", "chunks": len(self._chunks)}
+        return {"status": "ok", "chunks": len(self._chunks), "indexed": self._indexed}
```

**Why the fix works.** Content-addressing: each chunk's SHA-256 is the key to its embedding. On a re-index, every hash is already in `_vector_by_hash`, `todo` is empty, the loop body never runs: zero provider calls, zero tokens (`test_reindexing_an_unchanged_corpus_is_free`). An edited document produces new hashes only for its changed chunks, which are embedded (`test_changed_document_is_reindexed`), so the index cannot go stale. New chunks are paged in batches of MAX_BATCH, so the first index of ~44 chunks is 1 call instead of 44 (`test_indexing_batches_its_embedding_calls` allows at most n_chunks//100 + 2). Duplicate texts inside a batch are embedded once via `unique.setdefault`. Health no longer does cold-start work (that half is INC-001/Phase 19). Honest limit of the reference fix: `_vector_by_hash` is in-process, so a brand-new process after a deploy still pays one batched call; persisting the hash->vector map (e.g. the Phase 8 SQLite store) is what would make the deploy itself free.

**Concept.** Content-addressed incremental work: hash the input, skip what you have already paid for. And when an API takes a list, send a list — the provider charges and round-trips per call, not per item.

**Tempting wrong fix.** Batching alone: 44 calls -> 1 call per deploy, which fixes the call-volume spike, but still re-sends ~8,000 tokens per deploy per pod; the token line stays. Or skipping `index()` when `self._indexed` is already True: free re-index, but an edited doc is never picked up and the index silently goes stale (`test_changed_document_is_reindexed` exists precisely for that). Or keying on filename/mtime instead of content: misses same-mtime edits and re-embeds every chunk of a file when one paragraph changed.

#### 4. N+1 rerank: one flagship chat call per candidate instead of one batched call

**Why it hurts.** Eight candidates = eight separate generation calls per ask, every ask. On the harness that is 8 of the 11 provider calls per ask (the question is also re-sent 8 times, ~100 redundant tokens). Each call consumes one unit of rate-limit quota, one round-trip of latency, and in real APIs one fixed per-request overhead (system prompt, request minimums, per-call billing on rerank endpoints). This is the INC-001 latency bug seen through the cost lens: Phase 19 let you fix it either way; Phase 20's call budget (`budget_ask_provider_calls = 4`) only passes if the calls are REMOVED, not hidden.

**Before**

```
docsbot/service/pipeline.py:201-216 (origin/part4)
    with self.metrics.span("rerank"):
        scored: list[tuple[int, float]] = []
        for idx in candidate_ids:                 # rerank_top_n = 8
            passage = self._chunks[idx].text
            reply = self.provider.chat(
                f"SCORE:{question}|||{passage}",
                model=settings.chat_model,
                tag="rerank",
            )
            try:
                score = float(reply.strip())
            except ValueError:
                score = 0.0
            scored.append((idx, score))
```

**After**

```
docsbot/service/pipeline.py (diff)
+        if not candidate_ids:
+            return []
         with self.metrics.span("rerank"):
+            passages = [self._chunks[i].text for i in candidate_ids]
+            reply = self.provider.chat(
+                "SCOREMANY:" + question + "|||" + "|||".join(passages),
+                model=settings.chat_model,
+                tag="rerank",
+            )
+            scores: list[float] = []
+            for raw in reply.split(","):
                 try:
-                    score = float(reply.strip())
+                    scores.append(float(raw.strip()))
                 except ValueError:
-                    score = 0.0
-                scored.append((idx, score))
+                    scores.append(0.0)
+            scores += [0.0] * (len(candidate_ids) - len(scores))
+            scored = list(zip(candidate_ids, scores))
```

**Why the fix works.** The provider exposes a batch protocol (harness.py `_synthesize_reply`: `SCOREMANY:<q>|||<p1>|||<p2>...` -> comma-separated scores, computed by the same `_overlap_score` as SCORE, 'so batching the reranker cannot change the ranking — only its cost'). One call scores all 8 passages; parsing pads missing scores with 0.0 so a short reply cannot misalign indices. Per ask: 8 rerank calls -> 1, total 11 -> 4 (classify + embed + rerank + answer), which is exactly at budget. Fewer calls helps latency AND cost AND rate-limit headroom simultaneously.

**Concept.** Fewer calls beats faster calls. Parallelising N calls hides the latency but changes the cost, and the quota burn, by exactly nothing. Batching removes N-1 calls: latency, cost and rate limit all improve at once.

**Tempting wrong fix.** `ThreadPoolExecutor(max_workers=settings.rerank_concurrency)` over the 8 SCORE calls (config.py dangles `rerank_concurrency = 8` as bait, and the solutions file still imports ThreadPoolExecutor without using it for reranking). p95 drops ~8x, Phase 19 passes, and the bill is identical: still 8 calls, 8 quota units, 8x the 429 exposure under load. The Phase 20 test docstring literally says: 'Fewer calls beats faster calls ... Concurrency only helps the first.'

#### 5. Grounding on whole documents for ALL 8 candidates instead of the top_k retrieved chunks

**Why it hurts.** This is Marcus's 'enormous number of tokens per question' and the single largest per-ask token sink. A chunk is 800 chars ≈ 200 tokens; a handbook file is ~5,000 chars ≈ 1,250 tokens, so each source costs ~6x what the retrieved passage costs. Worse, the loop walks all 8 reranked candidates, not the top_k=4, and de-duplicates by source — so the prompt contains every distinct document any of the 8 candidates came from. With a 6-document corpus that is routinely 3–6 whole files ≈ 3,750–7,500 input tokens in the answer call, versus 4 x 200 = 800 tokens for the chunks retrieval actually judged relevant. In the worst case the 'retrieval-augmented' prompt contains the entire handbook, which makes retrieval and reranking pure overhead: you paid 9 calls to select passages and then sent everything anyway. The `load_corpus` docstring is explicit that the corpus is production-sized for this reason: 'when every document fits in a single chunk, send the whole document instead of the chunk costs nothing and teaches nothing.'

**Before**

```
docsbot/service/pipeline.py:225-242 (origin/part4)
    """We include the full source document for each retrieved chunk rather
    than the chunk alone, so the model always has surrounding context..."""
    ...
    seen: set[str] = set()
    for idx, _score in ranked:                       # all 8 reranked candidates
        chunk = self._chunks[idx]
        if chunk.source in seen:
            continue
        seen.add(chunk.source)
        full_doc = self._docs.get(chunk.source, chunk.text)   # line 241: whole file
        parts.append(f"[source: {chunk.source} #{chunk.index}]\n{full_doc}")
```

**After**

```
docsbot/service/pipeline.py (diff)
-        seen: set[str] = set()
-        for idx, _score in ranked:
+        for idx, _score in ranked[:settings.top_k]:
             chunk = self._chunks[idx]
-            if chunk.source in seen:
-                continue
-            seen.add(chunk.source)
-            full_doc = self._docs.get(chunk.source, chunk.text)
-            parts.append(f"[source: {chunk.source} #{chunk.index}]\n{full_doc}")
+            parts.append(f"[source: {chunk.source} #{chunk.index}]\n{chunk.text}")
```

**Why the fix works.** Send `chunk.text` for `ranked[:settings.top_k]`. The reranker already ordered candidates by model-judged relevance, so the top 4 chunks are the best evidence available; the answer prompt drops to ~900 tokens and total input per ask to ~2,500, under `budget_ask_input_tokens = 3200`. Citations were already computed from `ranked[:top_k]`, so the prompt and the citations now agree. Correctness guards (`assert_grounded`, `1 <= citations <= top_k`) still pass because the fake answerer cites the `[source: ...]` markers present in the prompt.

**Concept.** Context tokens are the bill. Retrieval's whole job is to pick the few paragraphs worth paying for; sending the whole file throws that selection away. 'Scoring 8 and grounding on the best 4 is the point of reranking.'

**Tempting wrong fix.** Shrink `chunk_size` to 'send fewer tokens' — changes nothing, because it is `full_doc`, not the chunk, that is sent. Or keep full docs but only for top_k — still ~4 x 1,250 = 5,000 tokens, over budget. The `seen` de-dup was itself the previous 'optimization', and it still shipped the whole corpus.

#### 6. Resending the entire transcript every turn (quadratic conversation cost, then context-window failure)

**Why it hurts.** Sam's datapoint. Let B be the base prompt (context + question) and h the tokens in one (user, assistant) pair. On the harness `_synthesize_reply` deliberately produces a ~1,000-char answer ('an unrealistically terse fake would hide the cost of resending a transcript entirely'), so h ≈ 250–260 tokens; in production with verbose answers h is easily 1,000+. Turn n re-sends all n-1 earlier pairs, so turn n costs B + (n-1)·h and a whole N-turn conversation costs N·B + h·N(N-1)/2 — quadratic in N. Turn 10 = B + 9h; at a lean B that is ~2x turn 1 on the harness and 'several times' in production where h is larger. And it ends badly: (n-1)·h eventually exceeds the context window, the provider returns a 400, and (INC-003 territory) a naive retry policy retries an error that cannot succeed. The spec measures this precisely: same question every turn so retrieval is constant, then turn 16 minus turn 11 — unwindowed drift = 5h ≈ 1,300 tokens (fails the `drift < 400` assertion); windowed = 0.

**Before**

```
docsbot/service/pipeline.py:231-233 (origin/part4)
    if history:
        for user_msg, assistant_msg in history:          # every turn so far
            parts.append(f"User: {user_msg}\nAssistant: {assistant_msg}")

pipeline.py:306-311: chat() passes self.sessions.history(session_id) — the full list of turns — into ask()
```

**After**

```
docsbot/service/pipeline.py (diff)
+        # INC-004: only the last N turns. Resending the whole transcript makes
+        # cost grow quadratically with conversation length and eventually
+        # overruns the context window outright.
         if history:
-            for user_msg, assistant_msg in history:
+            for user_msg, assistant_msg in history[-settings.history_window_turns:]:
                 parts.append(f"User: {user_msg}\nAssistant: {assistant_msg}")

(config.py: history_window_turns = 3; the SessionStore still records every turn, only the prompt is windowed)
```

**Why the fix works.** `history[-3:]` caps the resent transcript at 3 pairs, so from turn 4 onward every turn costs B + 3h — constant, and bounded below the context window regardless of conversation length. Turn 10 ≈ turn 4 ≈ 1.3x turn 1 on the harness, satisfying 'turn 10 costs no more than ~2x turn 1'. Memory is preserved (`test_conversation_still_has_memory` checks the session records both turns) and recent context still reaches the model; `sessions.history()` is untouched, so the window is a prompt-construction decision, not a data-loss one. (The comment tags it INC-004, but the spec that enforces it is Phase 20 and the ticket is INC-002.)

**Concept.** Anything you resend per request grows your cost as the square of conversation length. Bound what crosses the wire per turn (window, or summarize older turns) and the context window stops being a cliff.

**Tempting wrong fix.** Send no history at all: turn cost is flat and minimal, but the product loses memory and `test_conversation_still_has_memory` fails — 'the cheap fix ... is not allowed.' Truncating by raw character count instead of by turns cuts mid-message and can drop the user half of a pair. Measuring the defect the naive way (different questions, turn 10 vs turn 1) hides it: retrieval noise is several times larger than the history signal, so you conclude 'that's just how chat works.'

#### 7. Flagship model for a one-word yes/no classify (whose reply is not even read)

**Why it hurts.** The guard emits one token ('yes'/'no'). Per-token price is the lever for tiny calls, and the flagship is 5x the cheap model's price per token, so every classify line item is 5x what it needs to be. On the harness the dollar amount is small (~15 input tokens), but harness.py and config.py both call it 'the most common line item on a surprising bill' because in real systems the classify prompt carries a long system prompt and few-shot examples, and it runs on 100% of traffic, including traffic the cache should absorb. Note also that neither part4 nor the reference reads the reply: it is a call that costs money and changes nothing, which is why the spec explicitly allows deleting it.

**Before**

```
docsbot/service/pipeline.py:260-267 (origin/part4)
    # Is this even answerable from our docs? Cheap guard against
    # burning a full retrieval pass on "hi".
    with self.metrics.span("classify"):
        self.provider.chat(
            f"CLASSIFY:{question}",
            model=settings.chat_model,          # gemini-2.5-flash, the answer model
            tag="classify",
        )
    # return value discarded — the pipeline proceeds regardless
```

**After**

```
docsbot/service/pipeline.py (diff)
+            # INC-002: a yes/no guard does not need the flagship model.
+            with self.metrics.span("classify"):
+                self.provider.chat(f"CLASSIFY:{question}",
+                                   model=settings.cheap_model, tag="classify")

(config.py: cheap_model = gemini-2.5-flash-lite; harness.py prices it at CHEAP_COST_FACTOR = 0.2 and runs it at CHEAP_LATENCY_FACTOR = 0.4)
```

**Why the fix works.** Routing to `settings.cheap_model` cuts this call's cost 5x and its latency 2.5x with no quality impact, since a yes/no judgement is exactly what the small model is for. `test_trivial_calls_do_not_use_the_flagship_model` asserts every `tag="classify"` call uses `cheap_model` — and explicitly allows deleting the step, which would also be legitimate given the reply is unused (and would bring the ask to 3 calls). Moving the cache check ahead of it (root cause #2) means it no longer runs on cached traffic at all.

**Concept.** Right-size the model per call. Cost = sum over calls of (tokens x price-per-token-for-that-model); the flagship is for the answer, the small model for routing, classification and guards.

**Tempting wrong fix.** Shorten the classify prompt while leaving it on the flagship: the prompt is already ~15 tokens, so there is nothing to shorten; the 5x per-token multiplier is the cost. Parallelising classify with retrieval hides its latency and saves $0.

### Concepts to own

- Fewer calls beats faster calls: parallelising N calls fixes latency but leaves cost and rate-limit burn at N; batching into 1 call fixes latency, cost and quota simultaneously. Be able to say which Phase 19 fixes Phase 20 rejects and why (rerank_concurrency thread pool vs SCOREMANY).
- A cache only saves what comes after it, and a cache that never hits is invisible: check it before the expensive work, key it on exactly what the answer depends on, instrument hit rate, and treat 0% as an alarm (cache.py `age < ttl` vs `age >= ttl`; pipeline.py cache.get after 10 of 11 calls).
- Content-addressed incremental indexing: hash each chunk, embed only unseen hashes, page the rest through the batch endpoint (MAX_BATCH) — unchanged corpus = 0 calls, edited doc = only its changed chunks, and a health probe must never trigger the work.
- Context tokens are the bill: send the passages retrieval selected (top_k chunks, ~200 tokens each), not the whole file (~1,250) for every candidate; otherwise retrieval and reranking are overhead you pay before sending everything anyway.
- Conversation cost without windowing is quadratic: turn n = B + (n-1)h, N turns = N·B + h·N(N-1)/2, until (n-1)h overruns the context window; window (or summarise) the resent history so per-turn cost is constant — and know how to measure it (same question every turn, compare two late turns).
- Cost = sum(calls x tokens x model price): right-size the model for each call (cheap model for yes/no guards), and be able to explain why 3x traffic became a 12x bill — a dead cache removes economies of scale, per-deploy re-indexing scales with deploys and pods not users, and conversation cost scales with length squared.

### Interview questions

**Q: Traffic went up 3x but the bill went up 12x. Walk me through how that is even possible — shouldn't cost track traffic?**

Cost tracks traffic only if cost per request is constant, and three things made it grow instead. First, the answer cache had a 0% hit rate because of an inverted TTL comparison (`if age < ttl` evicted fresh entries and counted a miss), and it was consulted after classify, embed and 8 rerank calls anyway, so even a hit would have paid 10 of 11 calls. A working cache makes repeat traffic — the 'how do I export settlement reports' FAQ — nearly free, so cost should grow SUB-linearly with users; with a dead cache the repeat fraction that grows with scale is paid at full price. Second, every deploy and every first health probe re-embedded the whole corpus one chunk per call (~44 calls per pod), and deploy frequency grew with the team, not with users. Third, conversations resent the entire transcript every turn, so a conversation's cost grew with the square of its length, and usage shifted toward longer chats. On top of that the per-request base was already several times a lean pipeline: whole documents instead of chunks, 8 rerank calls instead of 1, flagship model for a yes/no. None of those scale linearly with users, so 12x on 3x is what you'd expect.

**Q: The cache was 'wired in correctly' and nobody noticed for a month. How is that possible, and how would you have caught it in week one?**

Because a cache is an optimisation, not a feature: when it fails, the product still returns correct answers, just at full price. Every correctness test passed. The only signal is the hit-rate metric, and nobody looked at it. The bug was a single comparison in cache.py — the stale check `if age < self.ttl_s:` dropped any entry YOUNGER than the TTL and counted a miss, then the caller re-put a fresh entry, so the clock reset on every ask and nothing asked more than once per 5 minutes could ever hit. I'd catch it with a spec like `test_repeated_question_costs_nothing_the_second_time` (ask twice, assert provider.call_count did not move) and a dashboard alert on `cache.hit_rate == 0` over any meaningful window. And I'd fix the second half too: move `cache.get` to the top of `ask()` so a hit costs zero calls, not one fewer.

**Q: Your colleague fixed the 8 serial rerank calls with a thread pool and p95 dropped 8x. Ship it?**

For INC-001, sure — Phase 19 accepts it. For INC-002, no. Parallelising 8 calls hides the latency but the provider still sees 8 requests, bills 8 requests, and debits 8 units of rate-limit quota, so the cost changes by exactly nothing and the 429 exposure under load gets worse. The provider exposes a batch protocol (SCOREMANY: one call, comma-separated scores, identical ranking because it is the same overlap function), so the right fix REMOVES 7 calls instead of hiding them: per ask 11 calls -> 4, which is exactly the call budget. Fewer calls beats faster calls because it improves latency, cost, and quota headroom at the same time; concurrency only helps the first.

**Q: Explain the 'enormous tokens per question' the VP saw on the dashboard, with numbers.**

The prompt builder sent the full source document for every distinct source among all 8 reranked candidates — `full_doc = self._docs.get(chunk.source, chunk.text)` over `ranked`, de-duped by filename. A chunk is 800 chars ≈ 200 tokens; a handbook file is ~5,000 chars ≈ 1,250 tokens, so each source cost ~6x what its passage would, and with 8 candidates over a 6-file corpus the prompt routinely contained 3–6 whole documents, 3,750–7,500 tokens — sometimes the entire handbook, which makes retrieval pointless. Add 8 SCORE prompts (~1,700 tokens) and a single ask was ~5.5k–9k input tokens. The fix sends `chunk.text` for `ranked[:top_k]`: 4 x 200 = 800 tokens of context, ~2,500 input tokens per ask total, under the 3,200 budget. The reranker already chose the best passages; sending the file throws that choice away.

**Q: A 10-turn conversation costs far more than 10x a 1-turn one. Is that just how chat works? How did you prove it and fix it?**

No. Each turn resent the whole transcript: turn n carries n-1 earlier (user, assistant) pairs at h tokens each (~260 on the harness, 1,000+ in production), so turn n costs B + (n-1)h and N turns cost N·B + h·N(N-1)/2 — quadratic — until (n-1)h overruns the context window and the provider returns a 400. Proving it needed a careful experiment: ask the SAME question every turn so retrieval is constant, then compare two late turns (16 vs 11) so the base prompt cancels; measured naively as turn 10 vs turn 1 with different questions, retrieval noise swamps the signal and you'd conclude it's 'just chat'. Unwindowed drift was ~5h ≈ 1,300 tokens; after `history[-settings.history_window_turns:]` (last 3 turns) it's 0, every late turn costs B + 3h, and turn 10 is ~1.3x turn 1. The session store still records every turn — only what crosses the wire is bounded — so memory is kept, and the spec forbids the cheap 'send no history' fix.

**Q: Why does re-indexing an unchanged corpus cost anything, and what does 'zero' require beyond batching?**

The old `index()` cleared `_chunks`/`_vectors` and called `embed([chunk.text])` once per chunk — ~44 round-trips and ~8,000 tokens per process start, per pod, and the /health probe triggered it lazily too, so every deploy was a call spike. Batching fixes the call count (44 -> 1, since 44 <= MAX_BATCH=100) but you still pay all the tokens every time. Zero requires content addressing: SHA-256 each chunk's text, keep a hash->embedding map, embed only hashes you haven't seen, and page those through the batch endpoint. Unchanged corpus: the to-do list is empty, zero calls. Edited doc: only its changed chunks embed, so the index can't go stale — which is why 'skip if already indexed' is the wrong fix. The reference keeps the map in-process; to make the deploy itself free you'd persist it.


---

## Phase 21 — Failure and waste under load

**Incident:** INC-003 — Provider blip, 30x spend, 90-second hangs

> **One-line story:** A 3-minute, 40% provider degradation became a 13-minute SEV1 at 30x spend and 90-second spinners because every resilience component in docsbot/service/resilience.py was hollow — is_retryable retried 400s, backoff_delay was deterministic so retries arrived as synchronized walls (20x volume), CircuitBreaker.allow() was `return True`, call_with_timeout was `return fn()`, and map_with_retry retried whole 50-item batches (success probability 0.6^50 ≈ 0) — so the reference diff made each one real (429/5xx/timeout-only retries, full-jitter `random.uniform(0, capped)`, a CLOSED/OPEN/HALF-OPEN breaker on monotonic time, a shared-pool `future.result(timeout=)`, per-item `retry_call`), bounding a non-retryable error to exactly 1 call, a dead-provider request to max_retries+1 = 5, a hanging call to timeout_s, and a whole outage to threshold + one probe per reset window (~11 calls for 3 minutes) regardless of traffic.

### What was reported

Priya Raman (SRE postmortem, incidents/INC-003-outage-cost-spike.md): 14:02 the model provider posts "partial degradation" — ~40% of requests return 429/503; the provider is degraded 14:02–14:05. 14:03 our latency goes from 2.5s to 90s+ — "Not errors. Hangs." 14:04 our outbound call volume to the provider rises ~20x while the provider is LESS available ("DDoSing a service that is already having a bad day"). 14:07 the provider recovers on their side (the ticket's timeline is slightly inconsistent: 3-minute degradation vs. recovery stamped 14:07); our error rate stays elevated another six minutes "working through a backlog of retries we generated ourselves". 14:13 recovered. Cost: ~30x normal hourly spend for a 3-minute upstream event, and fewer successful requests served than if we had returned errors immediately. Priya's list — four things plus "one more from reading the code": we retried 400s that cannot succeed; every client retried in lockstep ("we built a metronome"); nothing ever gave up on the provider ("no state anywhere in our code that represents 'the provider is down'"); nothing timed out (workers parked on sockets, starving users whose requests were fine); and the batch helper retried the whole batch when one item failed ("one failed item in a batch of fifty re-paid for the forty-nine that had already succeeded").

### How you measure it

Ground truth is `FakeProvider` in docsbot/perf/harness.py. `_precheck` (harness.py ~241-266) applies injected faults and, on a failure, still sleeps the normal latency and records a `CallRecord(ok=False, code=..., usd=self._usd(input_tokens, 0))` — the docstring says "A failed call still costs you a round-trip — that's the whole reason a retry storm is expensive." `hang_next(n, seconds)` makes the next n calls `time.sleep(seconds)` before answering. So the number to watch is `provider.call_count` (and `summary()['failed_calls']` / `['usd']`) relative to the number of LOGICAL requests. Concretely, from tests/test_phase21_failure.py: (1) `fail_next(10, code=400)` then one `retry_call` — call_count must be 1 and `sleep.delays == []` (broken: 5 calls, 4 sleeps of 0.5/1/2/4s); (2) `fail_next(1000, code=429)` — call_count == `settings.max_retries + 1` == 5; (3) `test_spend_during_an_outage_is_bounded`: 50 requests against `fail_next(10_000, code=503)` with a breaker — call_count <= `breaker_threshold + max_retries + 1` == 10 (fixed code: exactly 5; broken code: 50 x 5 = 250); (4) `len({backoff_delay(2) for _ in range(50)}) > 1` (broken: always 2.0); (5) `call_with_timeout(hangs, 0.05)` must raise TimeoutError in < 1.0s wall clock (broken: blocks the full 3-5s and returns normally); (6) 6 items, item 3 fails once -> `sum(attempts.values()) == 7` (broken: 4 + 6 = 10). Production equivalents: outbound-call rate / inbound-request rate (>> 1 during an outage = amplification), failed-call count and spend, worker-pool occupancy, and p99 latency pinned at "whatever the socket feels like".

### Root causes

#### 1. Retrying non-retryable errors (400/401/403/404/422 cost 5 calls instead of 1)

**Why it hurts.** A 400 describes OUR request, not the provider's health; the identical bytes produce the identical 400. With `settings.max_retries = 4` (docsbot/config.py:48) each malformed request costs (4+1) = 5 provider round-trips plus 0.5+1+2+4 = 7.5s of backoff sleep, and the harness bills input tokens on every failed call — 5x quota and 5x money for a guaranteed-zero outcome. Priya: 'A meaningful share of that call volume was 400s.' During the incident each one also holds a worker for 7.5s+ and adds to the breaker's failure run.

**Before**

```
docsbot/service/resilience.py:42-45 (origin/part4)
    code = getattr(exc, "code", None)
    if code is None:
        return False
    return True

The docstring directly above (lines 38-40) says: '400 = our request is malformed. 401 = our key is wrong. Retrying either one produces the identical failure, three more times, for money.' The body ignores it: ANY exception carrying a .code is retried.
```

**After**

```
+    if isinstance(exc, TimeoutError):
+        return True          # the request may simply have been unlucky
     code = getattr(exc, "code", None)
     if code is None:
         return False
-    return True
+    return code == 429 or 500 <= code < 600
```

**Why the fix works.** The fix makes the docstring true: only 429 (rate limited) and 5xx (their side broke) are transient. Other 4xx and any exception without a code (e.g. ValueError, a bug) re-raise on the first failure — `test_non_retryable_error_costs_exactly_one_call` asserts call_count == 1 and no sleeps. The new `TimeoutError` branch is required because the fixed `call_with_timeout` raises a plain TimeoutError with no .code; without it a single slow response would be classified permanent and surface to the user instead of getting one jittered retry.

**Concept.** Retry only what can change. A retry is a bet that the world will be different next time; for a deterministic failure the odds are zero and the stake is real money. Classify by status-class semantics — transient (429/5xx/timeouts) vs. permanent (4xx, bugs) — and fail permanent ones on attempt one.

**Tempting wrong fix.** `return code != 400` fixes the ticket's example but still retries 401/403/404/422 (the spec parametrizes over all five). `return isinstance(exc, ProviderError)` retries 400s too, because FakeProvider raises ProviderError for every injected code. Classification must be by status semantics, not exception type.

#### 2. Deterministic backoff -> synchronized retry waves (thundering herd; the 20x spike)

**Why it hurts.** A 429 comes from a per-interval rate limiter. If 500 requests fail at t0, broken backoff sends all 500 back at t0+0.5s within the same few milliseconds — the worst traffic shape for a limiter — so nearly all get 429 again, then all hit at t0+1.5, t0+3.5, t0+7.5. Exponential backoff lengthens the gaps but the burst never shrinks, and new traffic joins each wave. Arithmetic: with independent 40% failures, expected calls per request are 1+0.4+0.4^2+0.4^3+0.4^4 = 1.65x — nowhere near 20x. Getting to 20x needs the feedback loop: synchronized walls raise the provider's instantaneous load -> its failure rate for us climbs toward 100% -> every request costs the full 5 attempts -> each attempt is another wall. The retries cause the failures they are retrying. Priya: 'it isn't volume, it's synchronization.'

**Before**

```
docsbot/service/resilience.py:58 (origin/part4)
    return min(settings.backoff_base * (2 ** attempt), settings.backoff_max)

With docsbot/config.py:49-50 (backoff_base=0.5, backoff_max=8.0) EVERY caller at attempt n sleeps exactly 0.5, 1.0, 2.0, 4.0s. `import random` (line 18) is present and unused — the same 'imported, never used' tell as FutureTimeout. The docstring (lines 53-56) describes the bug: 'they all compute the same delay, all sleep the same duration, and all wake up and retry at the same millisecond.'
```

**After**

```
-    return min(settings.backoff_base * (2 ** attempt), settings.backoff_max)
+    capped = min(settings.backoff_base * (2 ** attempt), settings.backoff_max)
+    return random.uniform(0.0, capped)

(Docstring now: 'Uses FULL JITTER: a uniform random point in [0, capped_delay] ... Jitter spreads the retries out so the provider sees a smooth trickle instead of a wall.')
```

**Why the fix works.** Full jitter draws each delay uniformly from [0, capped]. 500 clients failing at t0 now retry spread across the whole 0.5s window (about 1/ms instead of 500/ms), then across 1s, 2s, 4s windows. The mean still doubles per attempt (`test_backoff_grows_with_attempt_on_average`, 200 samples at attempt 0 vs 3), it stays <= backoff_max (`test_backoff_is_bounded_by_the_cap`), and `test_backoff_is_jittered` requires > 1 distinct value across 50 calls. This is the AWS 'Exponential Backoff and Jitter' result: full jitter minimizes both total work and completion time under contention.

**Concept.** Backoff decides HOW LONG to wait; jitter decides WHEN everyone waits. Any deterministic function of shared inputs (same failure instant, same attempt number) gives every client the same output, so correlated clients need randomness to decorrelate. Spread, don't just delay.

**Tempting wrong fix.** A fixed per-process offset (`+ 0.1 * worker_id`) just shifts the wall; a narrow band (`capped + uniform(0, 0.05)`) keeps 500 retries inside 50ms, still a wall to a per-second limiter. Dropping max_retries to 1 halves the number of waves but not their width, and surfaces ordinary 429s as user errors. The jitter window must scale with the delay itself.

#### 3. Circuit breaker that never opens (no state for 'the provider is down, stop calling')

**Why it hurts.** Without a breaker, outage spend is PROPORTIONAL TO TRAFFIC: every incoming request pays up to max_retries+1 = 5 billed calls to a provider we already know is down. 50 requests -> 250 failed calls; 10,000 req/min -> 50,000 calls/min at a dead service. Priya: 'For eleven minutes we kept sending full traffic at a service we had overwhelming evidence was not answering.' Each user also waits through all 5 attempts and 7.5s of backoff before seeing an error that was knowable at t=0.

**Before**

```
docsbot/service/resilience.py:78-90 (origin/part4)
    def allow(self) -> bool:
        """True if a call may proceed."""
        with self._lock:
            return True
    ...
    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1

`_opened_at` (line 75) is initialised to None and nothing ever sets it, so `is_open` can never be true; `allow()` returns a constant under a lock. retry_call (lines 128-129) checks `breaker.allow()` every attempt against a function that cannot say no. docsbot/config.py:116-117 defines breaker_threshold=5 / breaker_reset_after_s=30.0 that nothing consumes.
```

**After**

```
def allow(self) -> bool:
         with self._lock:
-            return True
+            if self._opened_at is None:
+                return True
+            if time.monotonic() - self._opened_at >= self.reset_after_s:
+                # Half-open: let exactly one trial call through.
+                self._opened_at = None
+                self._consecutive_failures = self.threshold - 1
+                return True
+            return False
 ...
     def record_failure(self) -> None:
         with self._lock:
             self._consecutive_failures += 1
+            if self._consecutive_failures >= self.threshold and self._opened_at is None:
+                self._opened_at = time.monotonic()
```

**Why the fix works.** The breaker becomes a three-state machine. CLOSED: allow() True. On the `threshold`-th CONSECUTIVE failure (record_success zeroes the run — `test_success_resets_the_failure_run`) record_failure stamps `_opened_at = time.monotonic()` -> OPEN: allow() False, retry_call raises CircuitBreakerOpen before calling fn — zero provider calls, instant error. After `reset_after_s` the first allow() goes HALF-OPEN: clears `_opened_at`, sets `_consecutive_failures = threshold - 1`, returns True once. If the trial fails, record_failure reaches threshold and re-opens; retry_call's next iteration gets allow() False and raises CircuitBreakerOpen, so the probe costs exactly one call. If it succeeds, record_success closes the breaker. Outage spend is now bounded by TIME: threshold calls to open + one probe per reset window, independent of request volume (3-minute outage, threshold 5, reset 30s: about 5 + 6 = 11 calls whether 50 or 50,000 requests arrive). `test_spend_during_an_outage_is_bounded`: 50 requests -> 5 calls (ceiling 10). Honest caveat in the reference solution: 'exactly one trial' is not enforced under concurrency — between the half-open allow() and the trial's record_failure, other threads' allow() calls also see `_opened_at is None` and pass. A stricter breaker keeps an explicit HALF_OPEN state with a single permit.

**Concept.** Fail fast, and make 'down' a first-class, shared state. A retry is a per-request decision; a breaker is memory across requests about the dependency's health. It converts a slow cascading failure into a fast cheap one, protects the struggling dependency from you, and bounds spend by time (one probe per window) rather than by traffic.

**Tempting wrong fix.** (a) 'Set max_retries=0 during incidents' — still one call per request, still traffic-proportional, and needs a human. (b) A breaker that opens but never half-opens — never recovers after the provider does; `test_breaker_allows_a_trial_call_after_the_reset_window` exists for this. (c) Counting TOTAL rather than CONSECUTIVE failures — a healthy provider's 1% background errors eventually trip it. (d) time.time() instead of time.monotonic() — an NTP step can leave the breaker open forever or reopen early.

#### 4. No enforced timeout (the 90-second hangs; workers parked on dead sockets)

**Why it hurts.** A provider that returns 503 is cheap — you learn in ~50ms. A provider that ACCEPTS the connection and never answers is what takes a service down: fn() blocks indefinitely and the worker thread holds its pool slot. Note that in the broken code a hang never raises, so the retry machinery never even engages — there is no 5x multiplication, just an UNBOUNDED single wait. With N workers and hang rate h the pool fills at ~N*h per hang-duration; once all N are parked, users whose requests would have served instantly wait for a worker that never frees. That is the ticket's 'latency went to 90s+. Not errors. Hangs.' and 'users whose requests were completely fine also got nothing.' Parked workers plus the backlog of synchronized 429/503 retries are what stretched the incident six minutes past the provider's recovery. Priya: 'An error at 2 seconds is a vastly better product than a spinner at 90.'

**Before**

```
docsbot/service/resilience.py:103-111 (origin/part4)
    def call_with_timeout(fn: Callable[[], T], timeout_s: float | None) -> T:
        """Run `fn`, giving up after `timeout_s` seconds. ..."""
        return fn()

`timeout_s` is accepted and ignored. docsbot/config.py:51 defines `request_timeout: float = 30.0  # seconds per model call`, retry_call defaults `timeout_s=None` (line 118), and `from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout` (line 22) is imported and unused.
```

**After**

```
+# One shared pool so a timeout doesn't cost a thread spin-up per call.
+_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=32,
+                                   thread_name_prefix="docsbot-timeout")
 ...
-    return fn()
+    if timeout_s is None:
+        return fn()
+
+    future = _TIMEOUT_POOL.submit(fn)
+    try:
+        return future.result(timeout=timeout_s)
+    except FutureTimeout:
+        future.cancel()
+        raise TimeoutError(f"provider call exceeded {timeout_s}s")

(Docstring adds: 'Python cannot kill a running thread, so the abandoned call finishes in the background. This bounds what the *caller* waits for, which is what protects your worker pool. The complete fix is a timeout on the provider client's own socket — do both in real systems.')
```

**Why the fix works.** fn runs on a shared ThreadPoolExecutor and the CALLER waits at most `timeout_s` via `future.result(timeout=...)`; on expiry it raises a standard TimeoutError, which `is_retryable` now treats as transient (one jittered retry) and which the breaker counts as a failure, so sustained hangs trip it. `test_timeout_raises_instead_of_hanging` (5s hang, 0.05 timeout, < 1.0s wall) and `test_hanging_provider_call_does_not_park_the_caller` (`hang_next(1, seconds=3.0)`) verify it. The pool is created once at import (32 threads) to avoid a thread per call. The documented limitation is real: `future.cancel()` only stops a not-yet-started future; a running fn() keeps going on the pool thread, so under sustained hangs the 32-thread pool itself can fill. Hence 'do both': the executor bounds the caller's wait; an HTTP-client socket timeout (httpx/requests `timeout=`) bounds the resource.

**Concept.** Every outbound call needs a deadline, because the network's failure mode is not 'error' but 'silence'. A timeout turns an unbounded wait into a bounded one so a dependency's hang cannot consume your concurrency. Bound the WAIT (future timeout) and bound the RESOURCE (socket timeout) — they protect different things.

**Tempting wrong fix.** (a) signal.alarm/SIGALRM — main-thread only; raises ValueError from worker threads, exactly where these calls run. (b) Measuring elapsed time after fn() returns — the check never runs while it hangs. (c) A fresh threading.Thread per call with join(timeout) — pays a spin-up per call and leaks an unbounded number of parked threads. (d) Assuming `settings.request_timeout = 30.0` does anything — a configured limit nothing enforces is decoration (also the Phase 22 theme for TTLs).

#### 5. Wrong retry unit: map_with_retry retried the whole batch when one item failed

**Why it hurts.** Two costs. (1) Waste: if item 47 of 50 fails once, the comprehension raises, retry_call sleeps, and run_all restarts from item 0 — re-paying 46 already-successful calls whose results were discarded. (2) Liveness: with independent per-item failure p, the batch succeeds as a unit with probability (1-p)^B; at p=0.4, B=50 that is 0.6^50 ≈ 8e-12. Every attempt fails, each burns calls up to its first failure (expected ~1/p = 2.5, up to 50), all 5 attempts are used, and nothing is delivered — spend with zero output, which is exactly 'we served fewer successful requests than if we'd simply returned errors.' Spec's small case: 6 items, item 3 fails once -> broken makes 4 + 6 = 10 attempts; correct is 7.

**Before**

```
docsbot/service/resilience.py:161-164 (origin/part4)
    def run_all() -> list:
        return [fn(item) for item in items]

    return retry_call(run_all, sleep=sleep)

The retry wraps the list comprehension: the unit of retry is the entire batch.
```

**After**

```
-    def run_all() -> list:
-        return [fn(item) for item in items]
-
-    return retry_call(run_all, sleep=sleep)
+    # Retry the ITEM, not the batch. If item 47 of 50 fails once, retrying the
+    # whole batch re-pays for the 46 that already succeeded — and if failures
+    # are at all common, a large batch may never complete at all.
+    return [retry_call(lambda it=item: fn(it), sleep=sleep) for item in items]
```

**Why the fix works.** Each item gets its own retry_call, so a transient failure on item 3 retries only item 3; items 0-2 keep their results, items 4-5 run once. Per-item success within 5 attempts is 1 - 0.4^5 = 0.99, so a 50-item batch completes with probability ~0.99^50 ≈ 0.6 instead of ~0, at expected cost B x 1.65 = 82.5 calls instead of up to 250 for nothing. The `lambda it=item:` default-argument idiom binds the current item at definition time (a bare `lambda: fn(item)` would late-bind to the last item — though in this comprehension each lambda is consumed immediately, so it is defensive rather than load-bearing). `test_one_failing_item_does_not_rerun_the_whole_batch` asserts ordered results and total attempts == 7.

**Concept.** Make the unit of retry equal to the unit of failure. Retrying more than the thing that failed re-pays for finished work and multiplies the probability of never finishing. Fine-grained, idempotent retry is both cheaper and more likely to complete — the same logic as checkpointing.

**Tempting wrong fix.** 'Raise max_retries so the batch eventually succeeds' — P(batch succeeds) is 0.6^50 per attempt regardless of attempts; more attempts just cost more for the same ~0 outcome. Catching inside run_all and appending None for failures makes the batch 'succeed' with holes, silently corrupting downstream embed/rerank results.

### Concepts to own

- Retry amplification arithmetic: a permanently failing request costs max_retries+1 = 5 billed calls (the harness bills input tokens on ok=False calls); at independent failure rate p expected calls per request are 1+p+p^2+p^3+p^4 (1.65x at p=0.4) — the jump to 20x volume / 30x spend comes from feedback (synchronized retries push p toward 1) plus batch-unit retries (5 x 50 calls for zero output) plus traffic-proportional spend with no breaker.
- Transient vs. permanent errors: 429, 5xx and timeouts can change on retry; 4xx (400/401/403/404/422) and programming bugs cannot and must cost exactly one call — `code == 429 or 500 <= code < 600`, plus `isinstance(exc, TimeoutError)`.
- Backoff vs. jitter: exponential backoff sets how long; full jitter (`random.uniform(0, capped)`) sets when, decorrelating clients that failed together so a rate limiter sees a trickle, not a wall. Deterministic backoff is a synchronization bug ('a metronome'), not a volume bug.
- Circuit breaker state machine: CLOSED -> (threshold consecutive failures) -> OPEN (fail fast, zero provider calls) -> (reset_after_s on time.monotonic()) -> HALF-OPEN (one probe) -> CLOSED on success / OPEN on failure. Bounds outage spend by time (threshold + one probe per window, ~11 calls for 3 minutes) instead of by traffic.
- Timeouts bound the wait, socket timeouts bound the resource: `future.result(timeout=)` on a shared ThreadPoolExecutor guarantees the caller returns in timeout_s, but Python cannot kill the thread, so the abandoned call finishes in the background and the pool can still fill — set the HTTP client's own timeout too. In the broken code a hang never raised at all, so the wait was unbounded and retries never even engaged.
- Unit of retry = unit of failure: retrying a 50-item batch because item 47 failed re-pays 46 successes and succeeds with probability (1-p)^B ≈ 0 at p=0.4; per-item retry costs B x 1.65 and actually completes.

### Interview questions

**Q: The provider was degraded for three minutes at 40% failure. How did that become a 13-minute incident at 30x cost — where does the multiplication come from?**

Several multipliers stacked. Depth: max_retries was 4, so anything that kept failing cost 5 billed calls, and failed calls bill input tokens. Scope: the batch helper retried the whole batch, so one late failure in 50 re-paid ~49 successes per retry, and at 40% per-item failure a 50-item batch succeeds as a unit with probability 0.6^50 ≈ 1e-11 — all five attempts burned for nothing. Classification: 400s were retried, 5 calls per guaranteed failure. Synchronization: backoff was deterministic (0.5/1/2/4s), so everything that failed at t0 retried in the same millisecond — walls a rate limiter is guaranteed to reject — pushing the provider's failure rate for us from 40% toward 100%, which pushed every request to the full 5 attempts. Independent 40% failures would give only 1.65x; the feedback loop is what makes 20x. No breaker, so spend stayed proportional to incoming traffic for eleven minutes. And duration: no timeout meant hanging calls parked workers for as long as the socket stayed silent, starving healthy requests, while the backlog of synchronized retries kept the error rate up six minutes after the provider recovered.

**Q: Why isn't exponential backoff enough? It already spaces retries out.**

Backoff changes how long each client waits, but every client computes the same delay from the same inputs — same failure instant, same attempt number — so they wake together. 500 failures at t0 become 500 retries at exactly t0+0.5, t0+1.5, t0+3.5, t0+7.5. Gaps grow but each burst stays 500 wide, and a rate limiter keys on instantaneous rate, so each wall is 429'd again. The fix is full jitter: `random.uniform(0, capped)`. The mean still doubles per attempt and stays under backoff_max, but the 500 retries spread across the whole window — ~1/ms instead of 500/ms. A fixed offset or a tiny jitter band would not do it; the spread must be proportional to the delay.

**Q: State the bound the circuit breaker gives you on outage spend, and why lowering max_retries would not achieve the same thing.**

Without a breaker, spend is proportional to traffic: every request pays up to max_retries+1 calls to a dead provider — 50 requests = 250 calls, 50,000 = 250,000. With the breaker, after breaker_threshold=5 consecutive failures allow() returns False and retry_call raises CircuitBreakerOpen before touching the network. After reset_after_s=30s it half-opens and lets one probe through; if that fails it re-opens at once, one call per window. So the bound is threshold + ceil(outage_s / reset_after_s): about 5 + 6 = 11 calls for a 3-minute outage regardless of request volume. The spec asserts 50 requests against a dead provider cost <= 10; it is actually 5. max_retries=0 still pays one call per request — still traffic-proportional — and turns every ordinary 429 into a user error. The breaker is the only fix that decouples spend from traffic, because it is shared state about the dependency rather than a per-request decision.

**Q: The broken code already called breaker.allow() on every attempt and counted failures. What was actually wrong?**

allow() was `return True` under a lock, and nothing ever set `_opened_at`, so the count was dead state — the lock protected a constant. The fix is two hunks: record_failure stamps `_opened_at = time.monotonic()` when consecutive failures reach threshold (record_success resets the run, so it is consecutive, not cumulative — otherwise 1% background errors would eventually trip it); and allow() returns False while open, except that once reset_after_s has elapsed it clears `_opened_at`, sets the count to threshold-1 and returns True once — half-open — so one more failure re-opens and one success closes. Two review notes: monotonic time is correct because wall-clock steps would break it; and 'exactly one trial' is not enforced under concurrency, since between that allow() and the trial's record_failure other threads see the breaker closed — a stricter version keeps an explicit HALF_OPEN state with a single permit.

**Q: How did you implement the timeout, and what does it NOT protect you from?**

call_with_timeout was `return fn()` — it accepted timeout_s and ignored it, and `settings.request_timeout = 30.0` existed with nothing reading it; in the broken code a hang never raised, so the caller simply waited as long as the provider stayed silent and retries never engaged. The fix submits fn to a shared ThreadPoolExecutor created once at import (32 threads) and waits with `future.result(timeout=timeout_s)`; on expiry it raises TimeoutError, which is_retryable treats as transient and the breaker counts as a failure. That bounds what the caller waits for, which protects the request-serving pool and turns a 90-second spinner into a ~2-second error. What it does not do: Python cannot kill a thread, so future.cancel() is a no-op on a running call and the hung fn() finishes in the background; under sustained hangs the 32-thread pool can fill. The complete fix also sets a socket-level timeout on the HTTP client — the executor bounds the wait, the socket timeout bounds the resource. SIGALRM would not work because it only fires on the main thread.

**Q: Why change map_with_retry from retrying the batch to retrying each item? Give me numbers.**

The broken version wrapped `[fn(item) for item in items]` in one retry_call, so the unit was the whole batch. Waste: if item 47 of 50 fails once, we sleep and re-run from item 0, re-paying 46 calls we just discarded. Liveness: the batch succeeds only if every item does, probability (1-p)^B; at 40% and B=50 that is 0.6^50 ≈ 8e-12, so all five attempts fail and nothing is delivered — spend with zero output, why we served fewer successful requests than if we had failed immediately. Raising max_retries cannot fix it. Per-item retry makes each item succeed with 1 - 0.4^5 = 99%, the batch with ~60%, at expected cost B x 1.65 ≈ 82 calls instead of up to 250 for nothing. Spec's small case: 6 items, item 3 fails once — broken makes 10 attempts, fixed makes 7. Detail: `lambda it=item: fn(it)` binds each item at definition time, a defensive guard against Python's late-binding closures.


---

## Phase 22 — Leaks and the regression gate

**Incident:** INC-004 — The sawtooth (Priya Raman, SRE; scope added by Sam Ortiz, Eng Manager)

> **One-line story:** Pods OOM-killed every ~6 hours in a traffic-proportional sawtooth because the session store's TTL (purge_expired, never called) and cap (max_sessions, never read) were configured but not enforced and a naive metrics object appends samples forever; the mentor's diff purges expired sessions on every write, evicts least-recently-seen sessions past the cap after each insert, bounds each metric to max_samples by dropping the oldest, stops rebuilding the client per request and windows the prompt history to 3 turns — and then implements budgets.py so a CI perf-gate job (pytest -m phase4) compares p95, tokens, calls and dollars per ask from the harness against measured+~30% limits and fails the build worst-first, so the INC-001..003 fixes can no longer silently regress.

### What was reported

Priya attached a week of pod memory graphs: "It's a sawtooth. Memory climbs steadily for about six hours, hits the limit, the pod gets OOM-killed, restarts at baseline, and climbs again. Seven times a day, every day, since launch." Users mostly don't notice (a request in flight during the kill fails, they retry), so it sat at the bottom of the backlog for months. Two reasons to fix it now: (1) the climb rate scales with traffic, so at current growth the 6h cycle becomes a 90-minute cycle by Q3; (2) it restarts most often during the busiest hours because that is when it fills fastest — the failure is correlated with exactly the traffic they most care about. Her own poking: the session store "is configured with a TTL and a max size, and as far as I can tell neither one is ever actually applied to anything. There's a purge_expired method. Nothing calls it." And a warning about the Phase 18 metrics object: "if we just built a metrics object that appends a sample per request and never drops any, we've added a second leak ... in the code whose entire job is to tell us about problems like this. That would be a hell of a thing to page ourselves about." Sam's follow-up: "We've now fixed three of these — latency, cost, and the retry storm. In every case somebody measured, found the cause, fixed it, and moved on. Nothing we did stops any of it from coming back ... I don't want a wiki page with our target numbers on it. We had one of those. I want the numbers to be a test that fails the build." The ticket also requires a POSTMORTEM.md in the repo root.

### How you measure it

Leaks: the number is "does the container's size plateau or keep climbing as you push distinct keys through it." For the session store: `len(service.sessions)` (or `service.sessions.stats()` -> {"sessions", "total_turns"}) after appending N distinct session ids with N > max_sessions — broken code returns N, fixed code returns <= max_sessions (spec tests/test_phase22_leaks_and_gate.py: 200 appends with cap 10 leaves <=10; service-level 120 appends with cap 25 leaves <=25). For TTL: append, sleep past ttl_s (spec uses ttl_s=0.05, sleep 0.06), append a new id, expect the old one gone. `python -m docsbot.perf.report` prints "sessions held in memory: N" (report.py:181). For Metrics: `len(m.samples("ask_latency"))` after 20,000 `observe()` calls with max_samples=50 must be exactly 50; the phase18 spec (test_observe_is_bounded_and_drops_oldest) checks that after 1000 observes with cap 100, min==900.0 and max==999.0, i.e. the OLDEST were dropped. In production the same thing is the pod RSS curve: linear-with-traffic means a leak, flat-with-a-ceiling means bounded. The gate: `pytest -m phase4` (CI `perf-gate` job in .github/workflows/ci.yml; note pyproject.toml:52 `addopts = "-m 'not phase2 and not phase3 and not phase4'"` so plain `pytest` would NOT run it) runs `test_the_pipeline_is_within_every_budget`, whose `measure_ask()` clears `service.cache._data` before each of 5 asks and computes four numbers straight off `FakeProvider` (not from your own Metrics, so you cannot report your way to green): ask.p95_s <= settings.budget_ask_p95_s (0.130 s), ask.input_tokens <= 3200 per ask, ask.provider_calls <= 4 per ask, ask.usd <= 0.00034 per ask. A regression prints a sorted, worst-first list of `Violation` strings such as `ask.input_tokens: 12800 tokens exceeds budget 3200 tokens (4.00x)` plus the observed dict; `test_the_gate_actually_catches_a_regression` feeds 3x/4x/1x/2x values to prove the gate fires on exactly the three over-budget names and puts input_tokens (4x) first.

### Root causes

#### 1. Session TTL configured but never enforced (purge_expired is dead code)

**Why it hurts.** Every /chat call does `self.sessions.append(session_id, message, answer.text)` (pipeline.py:310 on part4). Each distinct session_id allocates a Session object plus a growing `turns` list of (user, assistant) strings — the assistant answer is the full generated text. There is no subtractive term anywhere: memory(t) = new_sessions_per_hour x bytes_per_session x t. That is a straight line, which is the rising edge of Priya's sawtooth; the vertical drop is the OOM-kill. Because the slope is proportional to traffic, peak hours fill the pod fastest (her observation 2) and traffic growth shortens the period (her observation 1: 6h -> 90 min). Idle conversations from this morning are still resident at 4pm even though the code says ttl_s=3600.

**Before**

```
docsbot/service/sessions.py:35-57 and :64-76 (origin/part4)

    def __init__(self, ttl_s: float = 3600.0, max_sessions: int = 1000) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
    ...
    def append(self, session_id, user_msg, assistant_msg):
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = Session(session_id=session_id)
                self._sessions[session_id] = sess
            sess.turns.append((user_msg, assistant_msg))
            sess.last_seen_at = time.monotonic()
    ...
    def purge_expired(self, *, now=None) -> int:
        """Drop sessions idle for longer than the TTL. ...
        Nothing calls this. It has never been called in production. It is,
        as far as the running process is concerned, decorative."""

On part4, `purge_expired` has exactly one occurrence in the package: its definition. `get_or_create` and `append` only insert into `_sessions`; no code path removes.
```

**After**

```
git diff origin/part4 origin/part4-solutions -- docsbot/service/sessions.py

     def get_or_create(self, session_id: str) -> Session:
+        self.purge_expired()
         with self._lock:
 ...
     def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
+        self.purge_expired()
         with self._lock:
 ...
-        Nothing calls this. It has never been called in production. It is,
-        as far as the running process is concerned, decorative.
+        Called on every write, so idle conversations cannot accumulate.
```

**Why the fix works.** Calling `purge_expired()` on every write path turns the configured TTL into an enforced one: any session idle > ttl_s is evicted the next time anyone writes, so the resident set is bounded by (sessions active in the last hour) rather than (sessions ever seen). It piggybacks on traffic — the busier the service, the more often it sweeps — which is the right direction for a leak that grows with traffic. Cost is an O(n) scan per write over at most max_sessions entries, trivial at n<=1000. `test_expired_sessions_are_purged_on_write` and `test_active_session_survives` (a session that keeps writing inside the TTL keeps all its turns) pin both halves. Note purge_expired is called BEFORE taking the lock in append/get_or_create because it takes the same non-reentrant `threading.Lock` itself.

**Concept.** Configured is not enforced. A limit that no code path applies is documentation, not a policy. The test for any `ttl`/`max_*` field is: grep for who READS it; if the answer is 'nobody', the limit does not exist.

**Tempting wrong fix.** Start a background thread/timer that calls purge_expired() every N minutes. It works until it doesn't: the thread dies silently on an exception, never starts in the worker model you deploy under, or sweeps too slowly for a burst. Sweep-on-write cannot be forgotten or die separately from the code path that creates the garbage. Also wrong: raising the pod memory limit — that changes the period of the sawtooth, not its shape.

#### 2. Session cap configured but never read (no eviction path at all)

**Why it hurts.** A TTL bounds the store only if the arrival rate of distinct ids within one TTL window is small. With ttl_s=3600, the worst-case resident set is (new ids per hour) x bytes — unbounded in the arrival rate. A client bug that mints a fresh uuid per retry, a load test, or a hostile caller produces 100k sessions inside the hour and the TTL never gets a chance to help. That is why the ticket separates 'TTL' and 'cap' as two distinct requirements: TTL bounds in time, cap bounds in space, and you need both for the memory curve to have a ceiling regardless of traffic shape.

**Before**

```
docsbot/service/sessions.py:36-37 (origin/part4)

        self.ttl_s = ttl_s
        self.max_sessions = max_sessions     # stored, never referenced again

No method in the part4 file compares `len(self._sessions)` to `self.max_sessions`. Contrast docsbot/service/cache.py:60-65 on the same branch, where `put()` DOES evict the oldest when `len(self._data) >= self.max_entries` — that container is bounded; this one only looks bounded.
```

**After**

```
+    def _enforce_cap(self) -> None:
+        """Hard ceiling on session count: evict least-recently-seen first.
+
+        TTL alone is not enough. A burst of 100k distinct session ids inside the
+        TTL window will still OOM you, and an attacker (or a retry loop with a
+        fresh uuid each time) can produce exactly that.
+        """
+        with self._lock:
+            overflow = len(self._sessions) - self.max_sessions
+            if overflow <= 0:
+                return
+            victims = sorted(self._sessions.items(),
+                             key=lambda kv: kv[1].last_seen_at)[:overflow]
+            for sid, _ in victims:
+                self._sessions.pop(sid, None)

and in get_or_create/append, after the `with self._lock:` block:
+        # Enforce AFTER inserting, or the cap is always off by one.
+        self._enforce_cap()
(in get_or_create the `return sess` is moved out of the lock so the cap runs first)
```

**Why the fix works.** `_enforce_cap` makes `len(_sessions) <= max_sessions` an invariant after every write, so peak memory is max_sessions x bytes_per_session — a constant independent of traffic. Eviction is least-recently-seen (sort by last_seen_at, drop the oldest `overflow`), so the conversations people are actively having survive (`test_cap_evicts_least_recently_seen_first`). It is called AFTER the insert so the ceiling is exactly max_sessions, and because it computes overflow from the current length it also reacts when `max_sessions` is lowered on a live store (`test_service_sessions_do_not_grow_without_bound` sets `service.sessions.max_sessions = 25` and pushes 120 ids). It is called outside `with self._lock` because it takes the same non-reentrant `threading.Lock` itself — calling it inside would deadlock.

**Concept.** Why a cap is needed even with a TTL: TTL bounds age, cap bounds count. Any container keyed by caller-controlled input (session id, user id, IP) must have a hard size ceiling, because the caller controls the arrival rate and you do not.

**Tempting wrong fix.** Check the cap BEFORE inserting and evict exactly one (`if len >= max: evict one; insert`). That is cache.py's pattern and works there, but it evicts at most one per write, so lowering max_sessions at runtime (which the phase22 service test does) leaves the store over the cap indefinitely. Enforcing after the write on the current length handles both. Also worth saying out loud: `sorted(...)` over the whole dict is O(n log n) per write — fine at n<=1000, but at max_sessions=1e6 you would want an OrderedDict/LRU.

#### 3. Metrics sample storage unbounded (the instrument that would have paged you)

**Why it hurts.** Arithmetic: ~5 span samples per ask x ~32 B per float-in-list (24 B float object + 8 B list slot) = ~160 B/request. At 10 req/s that is ~5.8 MB/hour from samples alone. The unbounded `_roots` list is worse: a Span dataclass per stage (~5 per ask) each with a children list, on the order of 1 KB+/request, tens of MB/hour at 10 req/s. Neither shows up in any functional test, both grow fastest under the heaviest load, and they live inside the object you would reach for to diagnose an OOM. Priya's line — 'a hell of a thing to page ourselves about' — is the point: observability code is on every request path and has to be held to the same bounded-resource standard as the service.

**Before**

```
docsbot/perf/metrics.py (origin/part4 — the Phase 18 stub, before implementation)

DEFAULT_MAX_SAMPLES = 4096
...
        self._roots: list[Span] = []          # completed top-level spans
        self._counters: dict[str, int] = {}
        self._samples: dict[str, list[float]] = {}
...
    def observe(self, name: str, value: float) -> None:
        """Record one histogram sample, keeping at most `max_samples` per name.
        When full, drop the OLDEST sample."""
        raise NotImplementedError("Phase 18: record a bounded sample.")

The naive implementation is `self._samples.setdefault(name, []).append(value)` — one float per sample, forever. `span()` must call `observe(name, duration)` on every exit (docstring requirement), so every request adds one sample per span (ask, classify, retrieve, rerank, generate, ...).
```

**After**

```
origin/part4-solutions docsbot/perf/metrics.py

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self._samples.setdefault(name, [])
            bucket.append(value)
            if len(bucket) > self.max_samples:
                # Drop oldest; recent behaviour is what we're debugging.
                del bucket[: len(bucket) - self.max_samples]

RESIDUAL the reference solution leaves open (same file, inside `span()`): 
            else:
                with self._lock:
                    self._roots.append(sp)
has no cap. Every top-level span tree (one `ask` root with its child spans per request) is retained forever, and `stage_table()` re-walks all of them via `_walk()` on every call. The specs only assert on `samples()`, so this passes `pytest -m phase22` and `-m phase18`, but by the ticket's own definition ('anything that grows without limit as traffic grows is a leak') it is one. A real fix is `collections.deque(maxlen=...)` for `_roots` or rolling the stage table up incrementally on span exit.
```

**Why the fix works.** Each metric name keeps at most `max_samples` (4096 by default), deleting from the FRONT so the retained window is the most recent behaviour — which is also what you want statistically: a p95 dominated by last Tuesday is not useful. Memory is bounded by (number of distinct metric names) x max_samples x ~32 B, about 130 KB per metric, a constant. Nearest-rank percentiles over the window still behave (`test_metrics_percentiles_still_work_when_bounded`: 5000 observes with cap 100). `test_metrics_sample_storage_is_bounded` pushes 20,000 samples through cap 50 and asserts exactly 50 remain; phase18's `test_observe_is_bounded_and_drops_oldest` asserts min==900, max==999 after 1000 observes with cap 100.

**Concept.** Bound your own instrumentation. Any per-request append into process memory must have a maxlen. A ring buffer / sliding window is the standard shape; recent-window percentiles are the feature, not a compromise.

**Tempting wrong fix.** Bound by wall-clock instead (keep the last 5 minutes). Under a traffic spike a time window is unbounded in count — the exact condition you are trying to survive. Bound by count, and if you want time semantics, also stamp samples. Also tempting: 'just call reset() periodically' — same failure mode as a background purge thread, and it throws away the data you wanted exactly when you wanted it.

#### 4. Per-request client rebuild and unbounded history on the prompt path (resource churn the diff also removes)

**Why it hurts.** Building a new SDK/HTTP client on every request throws away the connection pool: a socket/TLS handshake per call, file descriptors and pool objects that live until GC gets to them, and under concurrency a pile of half-closed connections. It is the same disease as the session store — a per-request allocation with no corresponding release path — just on the network side. The unbounded history loop is the per-session version of the same thing on the token side: prompt size grows linearly with turn count, so cumulative cost grows quadratically and at some turn the prompt exceeds the context window and the request simply fails. Note neither is asserted by the phase22 spec (no phase22 test reads `client_builds`); they are fixes the reference diff makes because the harness and ticket name them, and the token one is caught indirectly by the `ask.input_tokens` budget in multi-turn use.

**Before**

```
docsbot/service/pipeline.py:257-258 (origin/part4)

                # Fresh client per request keeps request state cleanly isolated.
                self.provider.build_client()

harness.py:155-157, 179-184 (ground truth, unchanged): "How many times a client was constructed. Phase 22 cares: rebuilding an HTTP client per request throws away connection pooling." / "each build means a fresh connection pool — see Phase 22." (`client_builds` counter, also exported in `provider.summary()`.)

docsbot/service/pipeline.py:231-233 (origin/part4)

        if history:
            for user_msg, assistant_msg in history:
                parts.append(f"User: {user_msg}\nAssistant: {assistant_msg}")
```

**After**

```
pipeline.py diff — the `build_client()` call is deleted outright (the fixed `ask()` never calls it), and:

+        # INC-004: only the last N turns. Resending the whole transcript makes
+        # cost grow quadratically with conversation length and eventually
+        # overruns the context window outright.
         if history:
-            for user_msg, assistant_msg in history:
+            for user_msg, assistant_msg in history[-settings.history_window_turns:]:

with config.py:109 `history_window_turns: int = 3` (already present on part4, unread until this diff).
```

**Why the fix works.** One client for the process lifetime means one pool, reused; `provider.client_builds` stops scaling with request count. Slicing `history[-N:]` bounds the prompt contribution of a conversation to a constant regardless of its length, so turn 10 costs roughly what turn 1 costs and the context window cannot be overrun by transcript. Honest limit: this bounds the PROMPT, not the STORE — `Session.turns` in sessions.py still grows for as long as one session keeps talking. It is bounded indirectly (TTL evicts idle sessions, cap evicts the oldest), but a single immortal chatty session can still grow; trimming `turns` to a window on append is the obvious follow-up and a good 'what would you do with one more day' answer.

**Concept.** Every per-request allocation needs a per-request (or bounded) release. Clients, pools, transcripts, and caches are all the same pattern: find the thing that grows by one per request and ask what makes it shrink.

**Tempting wrong fix.** Summarise old turns with another model call to 'keep the context'. That trades a memory/token bound for an extra provider call per turn — you just re-opened INC-002 to close INC-004. Start with the free fix (a slice); escalate only if quality measurably needs it.

#### 5. No executable budget — the numbers lived in a wiki, so INC-001..003 could silently regress

**Why it hurts.** Sam's paragraph is the mechanism: INC-001 (N+1 serial rerank, global lock, /health indexing), INC-002 (cache checked after the work, inverted TTL, whole-doc prompts, re-embed on deploy, flagship model for yes/no), INC-003 (retry 400s, lockstep backoff, no breaker, no timeout, batch-level retry) were each found by a human running the report once. A number in a wiki has no trigger; the next PR that puts `provider.chat()` back inside a `for idx in candidate_ids` loop passes every correctness test (answers are still right), lands, and the signal arrives as Marcus's invoice eight weeks later. The feedback loop is measured in billing cycles instead of minutes. That is why 'nothing stops any of this recurring' is the most important line in all four tickets: the three earlier fixes had a half-life, and the fourth ticket is the only one whose fix changes the half-life of all the others.

**Before**

```
docsbot/perf/budgets.py:51-81 (origin/part4)

    @property
    def overage_ratio(self) -> float:
        raise NotImplementedError("Phase 22: how far over are we?")
    def __str__(self) -> str:
        raise NotImplementedError("Phase 22: a one-line message naming the metric, observed, and limit.")

def default_budgets() -> list[Budget]:
    raise NotImplementedError("Phase 22: build the budget list from settings.")

def check(observed, budgets=None) -> list[Violation]:
    raise NotImplementedError("Phase 22: return the violations.")

Supporting pieces already present on part4 (unchanged by the diff): config.py:124-127 budget_ask_p95_s=0.130, budget_ask_input_tokens=3200, budget_ask_provider_calls=4, budget_ask_usd=0.00034; pyproject.toml:52 `addopts = "-m 'not phase2 and not phase3 and not phase4'"` (plain `pytest` never runs the gate); .github/workflows/ci.yml `perf-gate` job: `pip install -e ".[dev]"` then `pytest -m phase4`, with the comment 'If this turns flaky, widen the LATENCY budget only — the call-count, token, and cost budgets are deterministic and should never need slack.'
```

**After**

```
git diff origin/part4 origin/part4-solutions -- docsbot/perf/budgets.py

+        if self.budget.limit == 0:
+            return float("inf") if self.observed > 0 else 1.0
+        return self.observed / self.budget.limit
...
+        unit = f" {self.budget.unit}" if self.budget.unit else ""
+        return (f"{self.budget.name}: {self.observed:g}{unit} exceeds budget "
+                f"{self.budget.limit:g}{unit} ({self.overage_ratio:.2f}x)")
...
+    return [
+        Budget("ask.p95_s", settings.budget_ask_p95_s, "s"),
+        Budget("ask.input_tokens", settings.budget_ask_input_tokens, "tokens"),
+        Budget("ask.provider_calls", settings.budget_ask_provider_calls, "calls"),
+        Budget("ask.usd", settings.budget_ask_usd, "USD"),
+    ]
...
+    budgets = default_budgets() if budgets is None else budgets
+    violations = [
+        Violation(budget=b, observed=observed[b.name])
+        for b in budgets
+        if b.name in observed and observed[b.name] > b.limit
+    ]
+    violations.sort(key=lambda v: v.overage_ratio, reverse=True)
+    return violations

Consumed by tests/test_phase22_leaks_and_gate.py:172-209: `measure_ask()` does `provider.reset()`, clears `service.cache._data` before each of 5 asks, and builds observed = {p95 of wall clock, provider.input_tokens/n, provider.call_count/n, provider.usd/n} straight from the FakeProvider; `test_the_pipeline_is_within_every_budget` asserts `check(observed) == []` and prints every Violation plus the observed dict on failure.
```

**Why the fix works.** The four budgets are the four causal dimensions the earlier incidents regressed along — wall clock (INC-001), tokens (INC-002), call count (INC-001/002/003: the N+1 and the retry storm both show up here first), dollars (INC-002/003) — read from the harness, which is ground truth you may not edit. `check()` is pure and tiny so it is itself testable (`test_the_gate_actually_catches_a_regression` feeds 3x/4x/1x/2x and asserts the three violated names and input_tokens first; 'a gate nobody has ever seen fail is a gate nobody should trust'). Design choices each prevent a known failure mode of gates: exactly-at-limit passes (`>` not `>=`, no flake at the boundary); missing metric is skipped, not failed (a gate that fails on absent data trains people to ignore it); worst-first sort (fix the 5x before the 1.1x); zero-limit guarded so overage_ratio never divides by zero; message names metric, observed, limit, unit and ratio (the failure tells you what to do). Limits sit at measured + ~20-30%: 10x slack never fires, 0% slack fires on CI noise and gets disabled within a week, which is worse than no gate. Only the latency budget is noise-sensitive; calls/tokens/USD are deterministic against the fake provider. And the CI wiring matters as much as the code: because `addopts` deselects phase4, the `perf-gate` job must run `pytest -m phase4` explicitly — a gate that is not selected is the wiki page again.

**Concept.** A number in a wiki is a wish; a number in CI is a constraint. Turn every post-incident target into an executable check on the causal metric (calls, tokens, dollars, p95), sized at measured + 20-30%, run on every PR, with a failure message that names the metric and the overage.

**Tempting wrong fix.** Set the budgets from the BROKEN baseline 'so CI is green today and we tighten later'. Nobody tightens later; the gate is born with 10x slack and never fires. Equally wrong: gate on your own `Metrics` report instead of the harness counters — then a bug or a 'fix' in your instrumentation can make the gate pass. And: put the gate in the default `pytest` run with a 0% headroom latency budget — it flakes on a shared runner, someone adds `@pytest.mark.skip`, and you are back to folklore.

### Concepts to own

- Configured vs enforced limits: a ttl_s or max_sessions field that nothing reads is decoration. The audit is 'grep for who reads it'; a container is bounded only if there is a code path that removes from it, and that path runs without anyone remembering to call it (sweep-on-write beats a background timer).
- Why you need a cap even with a TTL: TTL bounds age, cap bounds count. A burst of distinct caller-controlled keys (fresh uuid per retry, a load test, an attacker) fills memory inside one TTL window. Evict least-recently-seen so active conversations survive; enforce after the insert on the current length so the ceiling is exact and survives a runtime change of the cap; don't take a non-reentrant lock twice.
- The sawtooth as a memory signature: linear climb whose slope is proportional to traffic, vertical drop at the OOM kill, period shrinking as traffic grows, worst during peak hours. Raising the limit changes the period, not the shape; only a subtractive term (eviction) flattens it.
- Bound your own instrumentation: anything that appends per request (samples, spans, logs, traces) needs a maxlen. Drop oldest; recent-window percentiles are what you want anyway. Know that the reference solution bounds _samples but leaves _roots unbounded — the spec does not catch it, the ticket's definition does.
- Executable budgets: gate on the causal metrics (calls/ask, tokens/ask, USD/ask, p95), read from ground truth you cannot edit, sized at measured + 20-30%, worst-first, skip-on-missing, pass-at-boundary, failure message naming metric/observed/limit/ratio, and actually selected by CI (here: a separate perf-gate job because addopts deselects phase4). Prove the gate fires with a synthetic regression.
- Why 'nothing stops any of this recurring' is the key line: INC-001..003 were fixed by a human who measured once and moved on; every one of those regressions passes every correctness test because answers stay correct. Without a gate the feedback loop is the invoice, eight weeks later. The postmortem plus the gate is what turns four fixes from folklore into a constraint the next engineer cannot silently undo.

### Interview questions

**Q: You're shown a memory graph that climbs for six hours, drops to baseline, climbs again. What do you conclude before opening any code, and what do you look for first?**

A sawtooth with a vertical drop is an OOM-kill-and-restart loop; the linear rising edge means something allocates per unit of traffic and nothing ever frees it. Two details refine it: the period shortens as traffic grows and fills fastest at peak, so the leak rate is proportional to request rate, not to time — rule out a slow timer-driven leak and look for per-request appends into process-lifetime containers. In DocsBot that is the session store (`self._sessions[session_id] = Session(...)` on every new id, with `purge_expired` defined but never called and `max_sessions` stored but never read) and the Phase 18 metrics object if `observe()` just appends. I would confirm by pushing N distinct session ids through and watching `len(store)` — it should plateau at the cap; in the broken code it equals N.

**Q: The store already has a TTL of one hour. Why did the mentor's fix add a hard cap as well — isn't the TTL enough?**

A TTL bounds the AGE of entries, not the COUNT. Peak residency is (distinct ids arriving within one TTL window) x bytes per session, and the arrival rate is controlled by callers, not by me: a client that mints a fresh uuid per retry, a load test, or an attacker puts 100k sessions in the dict inside the hour and the TTL never gets a turn. The cap (`_enforce_cap`: evict least-recently-seen until `len <= max_sessions`) makes peak memory a constant, max_sessions x bytes, independent of traffic shape. You need both: TTL so idle conversations leave, cap so a burst cannot OOM you. The spec encodes exactly this split — one test for purge-on-write, one that '200 ids with cap 10 leaves <= 10', and one that the most-recently-used session survives eviction.

**Q: Why call purge_expired() on every write instead of a background thread, and why is _enforce_cap() called outside the lock and after the insert?**

Sweep-on-write ties the cleanup to the code path that creates the garbage, so it cannot be forgotten, cannot die separately (a background thread that hits an exception stops sweeping silently), and scales its frequency with the thing causing growth. `_enforce_cap` and `purge_expired` both take `self._lock` themselves, and that lock is a plain `threading.Lock`, not an RLock, so calling either from inside the `with self._lock:` block in `append` would deadlock — hence purge runs before the block and cap after it. It runs AFTER the insert so the invariant is `len <= max_sessions` exactly — the diff's own comment says 'or the cap is always off by one' — and because it computes overflow from the current length, it also handles someone lowering `max_sessions` on a live store, which the phase22 service test does (sets it to 25, pushes 120 ids).

**Q: Priya said the metrics object could be 'a second leak'. What exactly was the risk, how was it bounded, and is the reference solution fully leak-free?**

`span()` calls `observe(name, duration)` on every exit, roughly five samples per request; a naive `list.append` keeps every float forever, ~160 B/request, growing fastest under the heaviest load — in the one object you would open to diagnose the OOM. The fix keeps at most `max_samples` (4096) per metric name and deletes from the front (`del bucket[: len(bucket) - self.max_samples]`), so memory is (metric names x 4096 x ~32 B) — a constant — and percentiles are over the recent window, which is what you want to debug anyway. Not fully: `self._roots.append(sp)` inside `span()` is still unbounded in the solution, one Span tree per top-level request, retained for the life of the process and re-walked by every `stage_table()` call. The spec only asserts on `samples()`, so it passes, but by the ticket's own definition it is a leak; I would make `_roots` a `deque(maxlen=...)` or roll the table up incrementally.

**Q: How does the CI gate work, and what would you say to someone who wants to set the budgets generously 'so the build stays green'?**

`budgets.py` has a `Budget(name, limit, unit)`, a `Violation` whose `__str__` names the metric, observed, limit and overage ratio, `default_budgets()` reading the four limits from settings (p95 <= 0.130 s, <= 3200 input tokens, <= 4 provider calls, <= $0.00034 per ask), and a pure `check(observed, budgets)` that returns violations worst-first, skips missing metrics, and passes at exactly the limit. The gate test runs five cold-cache asks (it clears the cache before each) and computes those four numbers straight from the FakeProvider's counters — ground truth you may not edit — so you cannot report your way to green. `test_the_gate_actually_catches_a_regression` feeds synthetic 3x/4x/2x overages to prove the gate fires and orders correctly. The CI workflow runs it as a dedicated `perf-gate` job with `pytest -m phase4`, which matters because `addopts` deselects phase4 by default. On slack: the limits are measured + ~20-30%. 10x slack never fires, including when it should — that is the wiki page with extra steps. 0% slack fires on runner noise and gets skipped within a week, which is worse than no gate because now people distrust gates. Only the latency budget is noise-sensitive; calls, tokens and dollars are deterministic against the fake provider and should never be widened.

**Q: Sam said the gate is 'the half I actually care more about'. Why is 'nothing stops any of this recurring' the most important sentence in all four tickets?**

Because it is the only sentence about the system that produced the bugs rather than about a bug. INC-001, 002 and 003 were each closed the same way: a person ran the report, found the cause, fixed it, moved on. Every one of those regressions — a `provider.chat()` back inside a loop, a whole document where a chunk would do, a retry on a 400 — passes every correctness test, because the answers are still right. So the next regression ships clean, and the signal arrives as Marcus's invoice eight weeks later. The gate changes the feedback loop from a billing cycle to a PR check and names the metric and the overage in the failure. Together with the postmortem, which records the wrong guesses and the reason each limit sits where it does, it is what turns four fixes from folklore that decays when the people who did it leave into a constraint the codebase enforces on whoever comes next.


---
