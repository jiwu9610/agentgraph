# Postmortem — DocsBot performance work

Template for the incident writeups; the completed version is
[PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md) in the repo root. Written for
an engineer who joins in six months and wonders why the code looks the way it
does.

Blameless means we describe what the system made easy to get wrong, not who got
it wrong. "The cache's TTL comparison was inverted" is a finding. "Sam wrote a
bug" is not, and it makes the next person hide theirs.

---

## Summary

Two or three sentences. What was wrong, what it cost, what it costs now. A
non-engineer should be able to read only this section and be correctly informed.

## Baseline

The numbers before you touched anything. Paste the actual report output.

| Metric | Before |
|---|---|
| p95 latency per ask | |
| provider calls per ask | |
| input tokens per ask | |
| USD per ask | |
| cost to re-index an unchanged corpus | |
| cache hit rate | |

---

## Per incident

Repeat for INC-001 through INC-004.

### INC-00N — <title>

**Symptom.** What a human observed. Not the cause — the thing that got reported.

**How you found it.** Which measurement pointed at the cause, and what you
initially suspected that turned out to be wrong. Include the wrong guess; it is
the most useful part of the document for the next reader.

**Cause.** The specific line or design decision. Be precise: `file.py:NN`.

**Why it was invisible.** Every one of these passed the test suite. Why? What
class of problem does the existing suite structurally not catch?

**Fix.** What you changed, and what you deliberately did *not* change.

**Proof.** The number, before and after.

---

## Results

| Metric | Before | After | Change |
|---|---|---|---|
| p95 latency per ask | | | |
| provider calls per ask | | | |
| input tokens per ask | | | |
| USD per ask | | | |
| re-index cost (unchanged corpus) | | | |
| cache hit rate | | | |

## What we changed about how we work

The fixes are the cheap part. What stops recurrence?

- Budgets now enforced in CI: list them and say why each limit is where it is.
- What a reviewer should now ask on any PR touching the request path.
- What you would still fix with another week, ranked, with rough estimates.

## Open items

Anything you found and consciously chose not to fix, with the reasoning.
Deferring is a legitimate engineering decision. Deferring silently is not.
