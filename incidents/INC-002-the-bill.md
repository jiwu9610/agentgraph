# INC-002 — "Why is this line item eleven thousand dollars"

*Scenario ticket: this incident was intentionally planted in the instrumented baseline as a production-debugging exercise; reporters are fictional personas. Measured resolution: [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md).*

**Opened by:** Marcus Oyelaran (VP Finance)
**Severity:** SEV2
**Status:** Resolved — root-cause analysis in [PART4_POSTMORTEMS.md](../PART4_POSTMORTEMS.md)

---

## What was reported

> I'm looking at the model provider invoice for last month and it's $11,400.
> Budget was $900. I approved $900 because that's what the projection said when
> we launched this.
>
> Traffic is up, I understand that. Traffic is up about 3x since launch. The
> bill is up 12x. Those numbers don't go together and I need someone to explain
> the gap to me before the board deck goes out on the 14th.
>
> I pulled what I could from the provider dashboard. Two things stood out to a
> non-engineer, so I assume they'll mean more to you:
>
> 1. We are apparently sending an enormous number of tokens per question. I
>    don't know what a normal number is. I know ours went up and nobody
>    shipped a feature that would obviously explain it.
> 2. There's a huge spike in call volume every time we deploy. We deploy
>    several times a day now. Is that supposed to happen?
>
> Also, Support tells me the most common question by far is "how do I export
> settlement reports" — same question, hundreds of times a week, presumably
> with the same answer. Are we paying full price every single time somebody
> asks that? Because that seems like something a computer should be able to
> figure out.

**Follow-up from Sam Ortiz (Eng Manager), same thread:**

> Adding one more datapoint since it's the same root question. Our longest
> support conversations are the expensive ones by a wide margin, and not
> proportionally — a 10-turn conversation costs way more than 10x a 1-turn
> conversation. Someone suggested that's just how chat works. I don't think
> that's just how chat works.

---

## What we know

- Cost per ask against the harness is roughly **6x** what a lean implementation
  of the same pipeline costs.
- The cache exists in the code and is wired in at the call site. Its measured
  hit rate in production is **0%**. Nobody noticed, because a cache that never
  hits is invisible — you still get correct answers, just at full price.
- Every deploy re-indexes the corpus, even when no document changed.
- Turn 10 of a conversation costs several times what turn 1 costs.

## Action items

Find every place this pipeline spends money it doesn't need to spend. There are
at least five distinct ones and they are worth very different amounts — rank
them before you fix them, and fix the expensive ones first.

Questions worth asking as you read:

- What does the cache key look like, and is the cache consulted *before* or
  *after* the work it's supposed to avoid?
- When we re-index a corpus where nothing changed, what should that cost? What
  does it cost?
- How many texts go into an embedding call, and how many *could*?
- When we ground an answer, are we sending the passage retrieval selected, or
  something considerably larger?
- Does every model call in this pipeline need the flagship model?
- What exactly grows as a conversation gets longer?

## Resolution criteria

(Verification: `pytest -m phase20` — correctness guards included.)

- Re-indexing an unchanged corpus costs **zero** provider calls.
- The same question asked twice costs **zero** provider calls the second time.
- Input tokens per ask are under `settings.budget_ask_input_tokens`.
- Turn 10 of a conversation costs no more than ~2x turn 1.
- You can give Marcus a one-paragraph answer, in plain English, with a number.
