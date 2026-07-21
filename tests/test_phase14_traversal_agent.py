"""Traversal agent specs — graph tools + path citations.

    pytest -m phase14

LLM is fully faked: a scripted decision sequence drives the tool loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.phase3, pytest.mark.phase14]

from docsbot.agent.budget import Budget
from docsbot.agent.runner import AgentAnswer, dispatch_tool, run_agent
from docsbot.agent.tools import follow_edge, get_neighbors, lookup_entity
from docsbot.kg.schema import Entity, GraphDelta, Provenance, Triple
from docsbot.kg.store import KnowledgeGraphStore


def _seed(store: KnowledgeGraphStore) -> None:
    prov = Provenance(
        source_path="org.md",
        content_hash="h",
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    ents = [
        Entity(type="Person", name="Mira Chen"),
        Entity(type="Person", name="Mira Chen Bot"),  # disambiguation hazard
        Entity(type="Service", name="payments-api"),
        Entity(type="Datastore", name="ledger-db"),
        Entity(type="Person", name="Sam Ortiz"),
        Entity(type="Incident", name="INC-1042"),
    ]
    triples = [
        Triple("Person:mira-chen", "OWNS", "Service:payments-api", prov),
        Triple("Person:mira-chen", "ONCALL_FOR", "Service:payments-api", prov),
        Triple("Person:sam-ortiz", "OWNS", "Datastore:ledger-db", prov),
        Triple("Incident:inc-1042", "CAUSED_BY", "Datastore:ledger-db", prov),
        Triple("Service:payments-api", "DEPENDS_ON", "Datastore:ledger-db", prov),
    ]
    # Fix entity ids for "Mira Chen Bot" and INC normalize
    ents = [
        Entity(type="Person", name="Mira Chen"),
        Entity(type="Person", name="Mira Chen Bot"),
        Entity(type="Service", name="payments-api"),
        Entity(type="Datastore", name="ledger-db"),
        Entity(type="Person", name="Sam Ortiz"),
        Entity(type="Incident", name="INC-1042"),
    ]
    store.upsert_delta(GraphDelta(entities=ents, triples=triples))


@pytest.fixture
def store(tmp_path: Path):
    s = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    _seed(s)
    yield s
    s.close()


def test_lookup_can_return_multiple(store):
    # Prefix / substring match must surface ambiguity (two Miras in the seed).
    hits = lookup_entity(store, "Mira")
    ids = {h["id"] for h in hits}
    assert "Person:mira-chen" in ids
    assert len(hits) >= 2


def test_neighbors_and_follow(store):
    hops = get_neighbors(store, "Person:mira-chen", predicate="OWNS")
    assert any(h["object_id"] == "Service:payments-api" for h in hops)
    owned = follow_edge(store, "Person:mira-chen", "OWNS")
    assert any(e["id"] == "Service:payments-api" for e in owned)


def test_dispatch_unknown_tool_raises(store):
    with pytest.raises(Exception):
        dispatch_tool("drop_production", {}, store)


def test_agent_returns_path_citation(store):
    """Script: lookup payments-api owners via inbound OWNS, then answer."""

    script = iter(
        [
            {
                "type": "tool",
                "id": "c1",
                "name": "get_neighbors",
                "args": {
                    "entity_id": "Service:payments-api",
                    "predicate": "OWNS",
                    "direction": "in",
                },
            },
            {
                "type": "final",
                "text": "Mira Chen owns payments-api.",
                "refused": False,
            },
        ]
    )

    def fake_llm(**kwargs):
        return next(script)

    ans = run_agent(
        "Who owns payments-api?",
        store,
        budget=Budget(max_hops=4, max_tool_calls=4, max_llm_calls=4),
        llm=fake_llm,
    )
    assert isinstance(ans, AgentAnswer)
    assert ans.refused is False
    assert "Mira" in ans.text
    assert any(
        h.predicate == "OWNS" and "payments-api" in h.object_id + h.subject_id
        for h in ans.path
    )


def test_agent_refuses_unknown_entity(store):
    script = iter(
        [
            {
                "type": "tool",
                "id": "c1",
                "name": "lookup_entity",
                "args": {"name": "billing-galaxy"},
            },
            {
                "type": "final",
                "text": "I cannot find billing-galaxy in the knowledge graph.",
                "refused": True,
            },
        ]
    )

    def fake_llm(**kwargs):
        return next(script)

    ans = run_agent(
        "Who owns billing-galaxy?",
        store,
        budget=Budget(max_hops=3, max_tool_calls=3, max_llm_calls=3),
        llm=fake_llm,
    )
    assert ans.refused is True
    assert ans.path == [] or all(
        "billing-galaxy" not in f"{h.subject_id}{h.object_id}" for h in ans.path
    )


def test_cycle_does_not_infinite_loop(store):
    """Agent revisiting an entity must not spin; budget/visited should stop it."""
    calls = {"n": 0}

    def flappy_llm(**kwargs):
        calls["n"] += 1
        if calls["n"] > 20:
            return {"type": "final", "text": "gave up", "refused": True}
        return {
            "type": "tool",
            "id": f"c{calls['n']}",
            "name": "get_neighbors",
            "args": {
                "entity_id": "Service:payments-api",
                "predicate": "DEPENDS_ON",
                "direction": "out",
            },
        }

    ans = run_agent(
        "walk forever",
        store,
        budget=Budget(max_hops=3, max_tool_calls=5, max_llm_calls=8),
        llm=flappy_llm,
    )
    assert calls["n"] <= 10
    assert ans is not None
