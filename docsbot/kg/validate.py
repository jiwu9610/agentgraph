"""Graph validation / quarantine helpers."""

from __future__ import annotations

from .schema import ENTITY_TYPES, RELATION_TYPES, GraphDelta, Triple


class SchemaError(ValueError):
    """Hard schema violation (unknown type, empty id, etc.)."""


def assert_closed_world_delta(delta: GraphDelta) -> None:
    """Raise SchemaError if any entity/triple uses an unknown type.

    Quarantined strings are allowed; bad rows should have been filtered before
    upsert — this is a belt-and-suspenders check before store.upsert_delta.

    Note: we check the *values*, not pydantic's validators — a Triple built via
    ``model_construct`` (or loaded from an old DB) bypasses validation entirely,
    and that is exactly the row this guard exists to catch.
    """
    for e in delta.entities:
        if e.type not in ENTITY_TYPES:
            raise SchemaError(f"unknown entity type: {e.type!r} on {e.name!r}")
    for t in delta.triples:
        if t.predicate not in RELATION_TYPES:
            raise SchemaError(
                f"unknown relation type: {t.predicate!r} on {t.subject_id!r}"
            )
        if not t.subject_id or not t.object_id:
            raise SchemaError(f"triple with empty endpoint: {t!r}")


def orphan_triple_ids(triples: list[Triple], known_entity_ids: set[str]) -> list[Triple]:
    """Return triples whose subject or object is not in known_entity_ids."""
    return [
        t
        for t in triples
        if t.subject_id not in known_entity_ids or t.object_id not in known_entity_ids
    ]


def is_known_entity_type(t: str) -> bool:
    return t in ENTITY_TYPES


def is_known_relation_type(t: str) -> bool:
    return t in RELATION_TYPES
