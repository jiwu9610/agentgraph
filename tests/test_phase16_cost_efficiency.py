"""Cost-efficiency specs — budgets, pre-call gates, caches.

    pytest -m phase16
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.phase3, pytest.mark.phase16]

from docsbot.agent.budget import Budget, BudgetExceeded, TurnCache
from docsbot.agent.runner import run_agent
from docsbot.kg.ingest import ingest_corpus
from docsbot.kg.schema import Entity, GraphDelta, Provenance, Triple
from docsbot.kg.store import KnowledgeGraphStore


def test_budget_check_tool_before_increment():
    b = Budget(max_tool_calls=1)
    b.record_tool()
    with pytest.raises(BudgetExceeded) as ei:
        b.check_tool()
    assert ei.value.kind == "tool_calls"


def test_budget_records_usd_from_tokens():
    b = Budget(max_usd=1.0, usd_per_1m_input=1.0, usd_per_1m_output=2.0)
    b.record_llm(input_tokens=1_000_000, output_tokens=1_000_000)
    assert b.input_tokens == 1_000_000
    assert b.output_tokens == 1_000_000
    assert b.usd_spent == pytest.approx(3.0)
    rem = b.remaining()
    assert "max_usd" in rem or "usd" in str(rem)


def test_budget_blocks_llm_when_tokens_exhausted():
    b = Budget(max_input_tokens=100, max_llm_calls=10)
    b.record_llm(input_tokens=100, output_tokens=0)
    with pytest.raises(BudgetExceeded):
        b.check_llm(estimated_input_tokens=1)


def test_turn_cache_memoizes_neighbors():
    cache = TurnCache()
    key = ("Service:payments-api", "DEPENDS_ON", "out")
    assert cache.get_neighbors(key) is None
    cache.put_neighbors(key, [{"object_id": "Service:ledger-service"}])
    assert cache.get_neighbors(key)[0]["object_id"] == "Service:ledger-service"


def test_agent_stops_on_budget_trap(tmp_path: Path):
    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    prov = Provenance(
        source_path="x.md",
        content_hash="h",
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.upsert_delta(
        GraphDelta(
            entities=[
                Entity(type="Service", name="payments-api"),
                Entity(type="Datastore", name="ledger-db"),
            ],
            triples=[
                Triple(
                    "Service:payments-api",
                    "DEPENDS_ON",
                    "Datastore:ledger-db",
                    prov,
                )
            ],
        )
    )

    n = {"i": 0}

    def wander(**kwargs):
        n["i"] += 1
        return {
            "type": "tool",
            "id": f"c{n['i']}",
            "name": "get_neighbors",
            "args": {
                "entity_id": "Service:payments-api",
                "predicate": "DEPENDS_ON",
                "direction": "out",
            },
        }

    budget = Budget(max_hops=2, max_tool_calls=2, max_llm_calls=10)
    with pytest.raises(BudgetExceeded):
        run_agent("wander", store, budget=budget, llm=wander)
    store.close()


def test_extract_cache_via_ingest_hash(tmp_path: Path):
    """Re-ingest unchanged file must not call extract (same contract as the KG ingest specs)."""
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "a.md").write_text("Mira Chen owns payments-api", encoding="utf-8")
    calls = {"n": 0}

    def extract(text, *, source_path, content_hash, **kwargs):
        calls["n"] += 1
        return GraphDelta(
            entities=[
                Entity(type="Person", name="Mira Chen"),
                Entity(type="Service", name="payments-api"),
            ],
            triples=[
                Triple(
                    "Person:mira-chen",
                    "OWNS",
                    "Service:payments-api",
                    Provenance(
                        source_path=source_path,
                        content_hash=content_hash,
                        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ),
                )
            ],
        )

    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    ingest_corpus(corpus, store, extract=extract)
    ingest_corpus(corpus, store, extract=extract)
    assert calls["n"] == 1
    store.close()
