"""Session specs — durable sessions + resume-after-checkpoint.

    pytest -m phase15
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.phase3, pytest.mark.phase15]

from docsbot.agent.budget import Budget
from docsbot.agent.runner import run_agent
from docsbot.agent.state import AgentCheckpoint, SessionStore
from docsbot.kg.schema import Entity, GraphDelta, Provenance, Triple
from docsbot.kg.store import KnowledgeGraphStore


@pytest.fixture
def store(tmp_path: Path):
    s = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    prov = Provenance(
        source_path="org.md",
        content_hash="h",
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    s.upsert_delta(
        GraphDelta(
            entities=[
                Entity(type="Person", name="Mira Chen"),
                Entity(type="Service", name="payments-api"),
            ],
            triples=[
                Triple(
                    "Person:mira-chen",
                    "OWNS",
                    "Service:payments-api",
                    prov,
                )
            ],
        )
    )
    yield s
    s.close()


def test_session_message_roundtrip(tmp_path: Path):
    ss = SessionStore(tmp_path / "sessions.sqlite")
    sid = ss.create_session("alice", ttl_seconds=60)
    ss.append_message(sid, "user", "Who owns payments-api?")
    ss.append_message(sid, "assistant", "Mira Chen")
    hist = ss.history(sid)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"].startswith("Who owns")
    ss.delete_session(sid)
    assert ss.history(sid) == []
    ss.close()


def test_checkpoint_roundtrip(tmp_path: Path):
    ss = SessionStore(tmp_path / "sessions.sqlite")
    sid = ss.create_session("alice")
    cp = AgentCheckpoint(
        session_id=sid,
        question="Who owns payments-api?",
        visited_entity_ids=["Service:payments-api"],
        path=[{"subject_id": "Person:mira-chen", "predicate": "OWNS", "object_id": "Service:payments-api"}],
        completed_tool_call_ids=["c1"],
        tool_results=[{"id": "c1", "name": "get_neighbors", "result": [{"x": 1}]}],
        status="running",
    )
    ss.save_checkpoint(cp)
    loaded = ss.load_checkpoint(sid)
    assert loaded is not None
    assert loaded.completed_tool_call_ids == ["c1"]
    assert loaded.path[0]["predicate"] == "OWNS"
    ss.close()


def test_resume_skips_completed_tool_calls(tmp_path: Path, store):
    ss = SessionStore(tmp_path / "sessions.sqlite")
    sid = ss.create_session("alice")
    ss.save_checkpoint(
        AgentCheckpoint(
            session_id=sid,
            question="Who owns payments-api?",
            completed_tool_call_ids=["c1"],
            tool_results=[
                {
                    "id": "c1",
                    "name": "get_neighbors",
                    "result": [
                        {
                            "subject_id": "Person:mira-chen",
                            "predicate": "OWNS",
                            "object_id": "Service:payments-api",
                        }
                    ],
                }
            ],
            path=[
                {
                    "subject_id": "Person:mira-chen",
                    "predicate": "OWNS",
                    "object_id": "Service:payments-api",
                }
            ],
            status="running",
        )
    )

    script = iter(
        [
            # Resume must not require re-deciding c1; next llm call can finalize.
            {"type": "final", "text": "Mira Chen owns payments-api.", "refused": False},
        ]
    )

    def fake_llm(**kwargs):
        return next(script)

    ans = run_agent(
        "Who owns payments-api?",
        store,
        budget=Budget(max_hops=4, max_tool_calls=4, max_llm_calls=4),
        session_store=ss,
        session_id=sid,
        llm=fake_llm,
        resume=True,
    )
    assert ans.refused is False
    assert "Mira" in ans.text
    # completed tool c1 must still be recorded (not wiped on resume)
    cp = ss.load_checkpoint(sid)
    assert cp is not None
    assert "c1" in cp.completed_tool_call_ids
    ss.close()


def test_compact_working_memory(tmp_path: Path):
    ss = SessionStore(tmp_path / "sessions.sqlite")
    sid = ss.create_session("alice")
    big = [{"id": f"c{i}", "name": "get_neighbors", "result": ["x" * 1000]} for i in range(6)]
    ss.save_checkpoint(
        AgentCheckpoint(
            session_id=sid,
            question="q",
            tool_results=big,
            visited_entity_ids=["Service:payments-api"],
            path=[{"subject_id": "a", "predicate": "OWNS", "object_id": "b"}],
            status="running",
        )
    )
    ss.compact_working_memory(sid, keep_last_tools=2)
    loaded = ss.load_checkpoint(sid)
    assert loaded is not None
    assert len(loaded.tool_results) <= 2
    assert loaded.visited_entity_ids == ["Service:payments-api"]
    assert loaded.path  # path retained
    ss.close()
