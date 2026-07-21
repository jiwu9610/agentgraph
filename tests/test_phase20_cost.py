"""Cost specs.  Run:  pytest -m phase20

Closes INC-002 (`incidents/INC-002-the-bill.md`).

Latency asks "how long did the user wait?". Cost asks "how many calls did we
make and how many tokens went into each?". They usually have the same answers,
but not always — parallelising eight calls fixes latency and changes cost by
exactly nothing. These specs are about *removing* work, not hiding it.
"""

from __future__ import annotations

import pytest

from conftest import assert_grounded
from docsbot.config import settings
from docsbot.perf.harness import MAX_BATCH

pytestmark = [pytest.mark.phase4, pytest.mark.phase20]


QUESTION = "Which service is the double-entry accounting core?"
OTHER_QUESTIONS = [
    "What happens when risk-engine times out?",
    "What does edge-gateway do?",
    "Which database uses synchronous replication?",
    "What is stored in Redis and why?",
    "How are webhooks retried?",
    "What is the reporting replica lag?",
    "Why is there no distributed transaction?",
    "What is written before an external call?",
    "Which service batches transactions for processors?",
    "What is the fallback processor policy?",
]


def _synthetic_corpus(n_docs: int = 8) -> dict[str, str]:
    """A corpus big enough that per-chunk embedding is obviously wrong."""
    return {
        f"doc{i}.md": (f"# Document {i}\n\n"
                       + f"Section about topic {i}. " * 200)
        for i in range(n_docs)
    }


# ----------------------------------------------------------------------
# the cache
# ----------------------------------------------------------------------
def test_repeated_question_costs_nothing_the_second_time(service, provider):
    """The single biggest cost lever: real traffic repeats itself constantly.

    INC-002: the cache was wired in correctly at the call site and had a 0% hit
    rate in production for a month. A cache that never hits is invisible — the
    answers are still correct, just at full price.
    """
    service.ask(QUESTION)
    calls_after_first = provider.call_count
    assert calls_after_first > 0, "the first ask should actually do work"

    service.ask(QUESTION)
    second_ask_calls = provider.call_count - calls_after_first

    assert second_ask_calls == 0, (
        f"asking the same question twice cost {second_ask_calls} extra provider "
        "calls; the cache is not serving it"
    )


def test_cache_hit_rate_is_reported(service):
    service.ask(QUESTION)
    service.ask(QUESTION)
    assert service.cache.hit_rate > 0, (
        f"cache stats show no hits at all: {service.cache.stats()}"
    )


def test_cache_is_consulted_before_the_expensive_work(service, provider):
    """A cache checked *after* retrieval and reranking saves almost nothing."""
    service.ask(QUESTION)
    provider.reset()
    answer = service.ask(QUESTION)
    assert provider.call_count == 0, (
        f"a cached answer still cost {provider.call_count} provider calls — the "
        "cache is being consulted too late in the request"
    )
    assert_grounded(answer)


# ----------------------------------------------------------------------
# indexing
# ----------------------------------------------------------------------
def test_reindexing_an_unchanged_corpus_is_free(fresh_service):
    """Deploys happen several times a day. Nothing changed. This must cost nothing."""
    corpus = _synthetic_corpus()
    fresh_service.index(corpus)
    fresh_service.provider.reset()

    fresh_service.index(corpus)
    assert fresh_service.provider.call_count == 0, (
        f"re-indexing an unchanged corpus cost "
        f"{fresh_service.provider.call_count} provider calls; hash the content "
        "and skip what hasn't changed"
    )


def test_changed_document_is_reindexed(fresh_service):
    """The flip side: a real edit MUST be picked up. Caching must not leave
    the index stale."""
    corpus = _synthetic_corpus(3)
    fresh_service.index(corpus)
    fresh_service.provider.reset()

    corpus["doc1.md"] = "# Document 1\n\n" + "Totally new content here. " * 200
    fresh_service.index(corpus)
    assert fresh_service.provider.call_count > 0, (
        "an edited document was not re-embedded — the index is now stale"
    )


def test_indexing_batches_its_embedding_calls(fresh_service):
    """The embedding endpoint charges one round-trip per CALL, not per text."""
    corpus = _synthetic_corpus(8)
    n_chunks = fresh_service.index(corpus)
    embed_calls = len(fresh_service.provider.calls_of("embed"))

    assert n_chunks > 20, "test corpus should produce plenty of chunks"
    expected_max = (n_chunks // MAX_BATCH) + 2
    assert embed_calls <= expected_max, (
        f"indexing {n_chunks} chunks took {embed_calls} embedding calls; batch "
        f"them (at most {expected_max} expected)"
    )


# ----------------------------------------------------------------------
# per-ask budgets
# ----------------------------------------------------------------------
def test_provider_calls_per_ask_within_budget(service, provider):
    """Fewer calls beats faster calls: it helps latency AND cost AND the rate
    limit simultaneously. Concurrency only helps the first."""
    service.ask(QUESTION)
    calls = provider.call_count
    assert calls <= settings.budget_ask_provider_calls, (
        f"{calls} provider calls per ask exceeds budget "
        f"{settings.budget_ask_provider_calls}: "
        f"{provider.summary()['calls_by_tag']}"
    )


def test_input_tokens_per_ask_within_budget(service, provider):
    """INC-002: we were grounding on whole documents instead of the chunk
    retrieval actually selected, and on every candidate instead of the best."""
    service.ask(QUESTION)
    tokens = provider.input_tokens
    assert tokens <= settings.budget_ask_input_tokens, (
        f"{tokens} input tokens per ask exceeds budget "
        f"{settings.budget_ask_input_tokens}"
    )


def test_usd_per_ask_within_budget(service, provider):
    service.ask(QUESTION)
    assert provider.usd <= settings.budget_ask_usd, (
        f"${provider.usd:.6f} per ask exceeds budget "
        f"${settings.budget_ask_usd:.6f}"
    )


def test_trivial_calls_do_not_use_the_flagship_model(service, provider):
    """A yes/no guard does not need the expensive model.

    Removing the call entirely also passes — that's a legitimate fix too.
    """
    service.ask(QUESTION)
    classify_calls = provider.calls_of("chat", tag="classify")
    for call in classify_calls:
        assert call.model == settings.cheap_model, (
            f"the classification step used {call.model!r}; route trivial calls "
            f"to {settings.cheap_model!r} or drop the step"
        )


# ----------------------------------------------------------------------
# conversations
# ----------------------------------------------------------------------
def test_conversation_cost_stops_growing_with_length(service, provider):
    """INC-002: 'a 10-turn conversation costs way more than 10x a 1-turn one.'

    Note the experiment design — it matters as much as the numbers. We ask
    the *same* question every turn, so retrieval returns the same chunks every
    time and the only thing changing is how much conversation we resend. Then we
    compare two LATE turns against each other rather than late-vs-first, which
    cancels the base prompt out of the measurement entirely.

    Measured the naive way — different questions, turn 10 vs turn 1 — this defect
    is invisible: different questions retrieve different amounts of context, and
    that noise is several times larger than the signal.

    Windowed history: late turns cost the same as each other. Unwindowed: every
    turn carries the whole transcript, so cost climbs forever and eventually
    overruns the context window outright.
    """
    per_turn: list[int] = []
    for _ in range(16):
        before = provider.input_tokens
        service.chat("sess-cost", QUESTION)
        per_turn.append(provider.input_tokens - before)

    drift = per_turn[15] - per_turn[10]
    assert drift < 400, (
        f"turn 16 cost {drift} more input tokens than turn 11 — conversation "
        f"cost is still growing with length, so window the history you resend. "
        f"Per-turn: {per_turn}"
    )


def test_conversation_still_has_memory(service):
    """The cheap fix — send no history at all — is not allowed."""
    service.chat("sess-mem", OTHER_QUESTIONS[0])
    service.chat("sess-mem", OTHER_QUESTIONS[1])
    history = service.sessions.history("sess-mem")
    assert len(history) == 2, "the session is not recording turns"


# ----------------------------------------------------------------------
# correctness guards
# ----------------------------------------------------------------------
def test_answers_are_still_grounded_after_cost_work(service):
    for question in OTHER_QUESTIONS[:4]:
        service.cache._data.clear()
        assert_grounded(service.ask(question))


def test_still_grounds_on_multiple_sources_when_relevant(service):
    answer = service.ask(QUESTION)
    assert 1 <= len(answer.citations) <= settings.top_k, (
        f"expected up to {settings.top_k} citations, got {len(answer.citations)}"
    )
