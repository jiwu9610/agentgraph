"""Closed ontology + graph value types.

These types are the contract: extract, store, agent tools, and evals all speak
this language. Do not invent parallel dict shapes.

Closed world means: if it is not in ENTITY_TYPES / RELATION_TYPES, it does not
enter the graph. Production KG pipelines fail closed on schema drift so a bad
prompt cannot silently invent `OWNS_KINDA` edges.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EntityType = Literal[
    "Person",
    "Team",
    "Service",
    "Datastore",
    "Incident",
    "Customer",
    "Runbook",
]

RelationType = Literal[
    "OWNS",
    "MEMBER_OF",
    "DEPENDS_ON",
    "CAUSED_BY",
    "IMPACTS",
    "MITIGATED_BY",
    "ONCALL_FOR",
    "DOCUMENTED_IN",
]

ENTITY_TYPES: frozenset[str] = frozenset(
    ("Person", "Team", "Service", "Datastore", "Incident", "Customer", "Runbook")
)
RELATION_TYPES: frozenset[str] = frozenset(
    (
        "OWNS",
        "MEMBER_OF",
        "DEPENDS_ON",
        "CAUSED_BY",
        "IMPACTS",
        "MITIGATED_BY",
        "ONCALL_FOR",
        "DOCUMENTED_IN",
    )
)


def normalize_name(name: str) -> str:
    """Canonical display/key form: trim, collapse whitespace, casefold."""
    return re.sub(r"\s+", " ", name.strip()).casefold()


def entity_id(entity_type: EntityType | str, name: str) -> str:
    """Stable primary key: ``{type}:{normalized_name}``.

    Must be deterministic across processes. Changing this function invalidates
    an existing DB — treat it like a migration.
    """
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity type: {entity_type!r}")
    norm = normalize_name(name)
    if not norm:
        raise ValueError("entity name is empty after normalize")
    # slug: keep alnum, turn other runs into single hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return f"{entity_type}:{slug}"


class Provenance(BaseModel):
    """Where a fact came from — non-negotiable for debugging wrong answers."""

    source_path: str
    content_hash: str
    extracted_at: datetime


class Entity(BaseModel):
    type: EntityType
    name: str
    attrs: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return entity_id(self.type, self.name)

    @field_validator("type")
    @classmethod
    def _type_closed(cls, v: str) -> str:
        if v not in ENTITY_TYPES:
            raise ValueError(f"unknown entity type: {v!r}")
        return v


class Triple(BaseModel):
    subject_id: str
    predicate: RelationType
    object_id: str
    provenance: Provenance

    def __init__(  # noqa: D107 — allow positional Triple(s, p, o, prov); tests use it
        self,
        subject_id: str | None = None,
        predicate: str | None = None,
        object_id: str | None = None,
        provenance: Provenance | None = None,
        **data: object,
    ) -> None:
        for key, val in (
            ("subject_id", subject_id),
            ("predicate", predicate),
            ("object_id", object_id),
            ("provenance", provenance),
        ):
            if val is not None:
                data[key] = val
        super().__init__(**data)

    @field_validator("predicate")
    @classmethod
    def _pred_closed(cls, v: str) -> str:
        if v not in RELATION_TYPES:
            raise ValueError(f"unknown relation type: {v!r}")
        return v


class GraphDelta(BaseModel):
    """One extraction unit: entities + triples from a single source doc."""

    entities: list[Entity] = Field(default_factory=list)
    triples: list[Triple] = Field(default_factory=list)
    quarantined: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons for rejected facts (schema drift, etc.)",
    )
