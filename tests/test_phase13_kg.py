"""Knowledge graph specs — typed KG schema, store, validate, idempotent ingest.

    pip install -e ".[dev,kg]"
    pytest -m phase13

No Gemini key required: extract is injected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.phase3, pytest.mark.phase13]

from docsbot.kg.ingest import file_content_hash, ingest_corpus
from docsbot.kg.schema import (
    ENTITY_TYPES,
    Entity,
    GraphDelta,
    Provenance,
    Triple,
    entity_id,
    normalize_name,
)
from docsbot.kg.store import KnowledgeGraphStore
from docsbot.kg.validate import SchemaError, assert_closed_world_delta, orphan_triple_ids


def _prov(path: str = "org.md", h: str = "abc") -> Provenance:
    return Provenance(
        source_path=path,
        content_hash=h,
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_entity_id_stable_and_normalized():
    assert normalize_name("  Mira   Chen ") == "mira chen"
    a = entity_id("Person", "Mira Chen")
    b = entity_id("Person", "mira chen")
    assert a == b == "Person:mira-chen"
    assert entity_id("Service", "payments-api") == "Service:payments-api"
    with pytest.raises(ValueError):
        entity_id("Spaceship", "x")  # type: ignore[arg-type]


def test_closed_world_rejects_unknown_types():
    bad = GraphDelta(
        entities=[Entity(type="Person", name="Mira Chen")],
        triples=[
            Triple(
                subject_id="Person:mira-chen",
                predicate="OWNS",  # ok
                object_id="Service:payments-api",
                provenance=_prov(),
            )
        ],
    )
    # Force an illegal predicate through model_construct to bypass pydantic
    evil = Triple.model_construct(
        subject_id="Person:mira-chen",
        predicate="SORTA_OWNS",
        object_id="Service:payments-api",
        provenance=_prov(),
    )
    bad.triples.append(evil)
    with pytest.raises(SchemaError):
        assert_closed_world_delta(bad)


def test_orphan_detection():
    t = Triple(
        subject_id="Person:mira-chen",
        predicate="OWNS",
        object_id="Service:missing",
        provenance=_prov(),
    )
    orphans = orphan_triple_ids([t], {"Person:mira-chen"})
    assert orphans == [t]


def test_store_upsert_lookup_neighbors(tmp_path: Path):
    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    mira = Entity(type="Person", name="Mira Chen")
    svc = Entity(type="Service", name="payments-api")
    delta = GraphDelta(
        entities=[mira, svc],
        triples=[
            Triple(
                subject_id=mira.id,
                predicate="OWNS",
                object_id=svc.id,
                provenance=_prov("org.md", "h1"),
            )
        ],
    )
    store.upsert_delta(delta)

    assert store.get_entity(mira.id).name == "Mira Chen"
    hits = store.lookup("mira chen")
    assert len(hits) == 1 and hits[0].id == mira.id
    outs = store.neighbors(mira.id, predicate="OWNS", direction="out")
    assert len(outs) == 1 and outs[0].object_id == svc.id
    owned = store.follow_edge(mira.id, "OWNS")
    assert [e.id for e in owned] == [svc.id]
    store.close()


def test_multi_provenance_same_fact(tmp_path: Path):
    """Same triple from two files should remain attributable to both sources."""
    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    ents = [
        Entity(type="Person", name="Mira Chen"),
        Entity(type="Service", name="payments-api"),
    ]
    t1 = Triple(
        subject_id="Person:mira-chen",
        predicate="OWNS",
        object_id="Service:payments-api",
        provenance=_prov("org.md", "h1"),
    )
    t2 = Triple(
        subject_id="Person:mira-chen",
        predicate="OWNS",
        object_id="Service:payments-api",
        provenance=_prov("services.md", "h2"),
    )
    store.upsert_delta(GraphDelta(entities=ents, triples=[t1]))
    store.upsert_delta(GraphDelta(entities=ents, triples=[t2]))
    triples = [
        t
        for t in store.all_triples()
        if t.subject_id == "Person:mira-chen" and t.predicate == "OWNS"
    ]
    sources = {t.provenance.source_path for t in triples}
    assert sources == {"org.md", "services.md"}
    store.close()


def test_ingest_is_idempotent_and_skips_unchanged(tmp_path: Path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    doc = corpus / "org.md"
    doc.write_text("# Mira Chen owns payments-api\n", encoding="utf-8")

    calls = {"n": 0}

    def fake_extract(text, *, source_path, content_hash, **kwargs):
        calls["n"] += 1
        return GraphDelta(
            entities=[
                Entity(type="Person", name="Mira Chen"),
                Entity(type="Service", name="payments-api"),
            ],
            triples=[
                Triple(
                    subject_id="Person:mira-chen",
                    predicate="OWNS",
                    object_id="Service:payments-api",
                    provenance=Provenance(
                        source_path=source_path,
                        content_hash=content_hash,
                        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ),
                )
            ],
        )

    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    r1 = ingest_corpus(corpus, store, extract=fake_extract)
    r2 = ingest_corpus(corpus, store, extract=fake_extract)
    assert r1.extracted == ["org.md"] or any(p.endswith("org.md") for p in r1.extracted)
    assert calls["n"] == 1  # second ingest must not re-extract
    assert any(p.endswith("org.md") for p in r2.skipped)
    assert store.document_hash(str(Path("org.md"))) in {
        file_content_hash(doc.read_text(encoding="utf-8")),
        None,
    } or store.document_hash("org.md") == file_content_hash(
        doc.read_text(encoding="utf-8")
    )
    store.close()


def test_soft_delete_removed_source(tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")

    def fake_extract(text, *, source_path, content_hash, **kwargs):
        name = "Alpha" if source_path.endswith("a.md") else "Beta"
        return GraphDelta(
            entities=[
                Entity(type="Person", name=name),
                Entity(type="Service", name="payments-api"),
            ],
            triples=[
                Triple(
                    subject_id=entity_id("Person", name),
                    predicate="OWNS",
                    object_id="Service:payments-api",
                    provenance=Provenance(
                        source_path=source_path,
                        content_hash=content_hash,
                        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ),
                )
            ],
        )

    store = KnowledgeGraphStore(tmp_path / "kg.sqlite")
    ingest_corpus(corpus, store, extract=fake_extract)
    b.unlink()
    report = ingest_corpus(corpus, store, extract=fake_extract)
    assert any(p.endswith("b.md") for p in report.deleted)
    remaining_sources = {t.provenance.source_path for t in store.all_triples()}
    assert not any(p.endswith("b.md") for p in remaining_sources)
    store.close()


def test_entity_types_cover_northstar_ontology():
    assert {
        "Person",
        "Team",
        "Service",
        "Datastore",
        "Incident",
        "Customer",
        "Runbook",
    } <= ENTITY_TYPES
