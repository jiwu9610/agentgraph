"""LLM extraction into a typed GraphDelta.

Gemini is called with a response_schema (RawDelta, a close intermediate of
GraphDelta) so the model returns structured facts. The pipeline then:
  1. Validates every type against ENTITY_TYPES / RELATION_TYPES
  2. Rewrites subject/object names into entity_id(...)
  3. Attaches Provenance(source_path, content_hash, extracted_at)
  4. Quarantines (does not raise for every bad row) when a single fact is
     malformed, but keeps hard-failing if the whole payload is unusable

Inject `generate` in tests — do not call the network from unit tests.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .schema import (
    ENTITY_TYPES,
    RELATION_TYPES,
    Entity,
    GraphDelta,
    Provenance,
    Triple,
    entity_id,
)


class RawEntity(BaseModel):
    """What we ask the model for: names, not ids — ids are OUR job (Step 2)."""

    type: str = Field(description="One of the allowed entity types.")
    name: str = Field(description="The entity's name exactly as written in the text.")


class RawTriple(BaseModel):
    subject_type: str
    subject_name: str
    predicate: str = Field(description="One of the allowed relation types.")
    object_type: str
    object_name: str


class RawDelta(BaseModel):
    entities: list[RawEntity] = Field(default_factory=list)
    triples: list[RawTriple] = Field(default_factory=list)


# Direction contract per relation: (allowed subject types, allowed object types).
# An LLM will happily emit "Runbook MITIGATED_BY Datastore"; the schema won't.
_RELATION_SIGNATURES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "OWNS": (frozenset({"Person", "Team"}), frozenset({"Service", "Datastore"})),
    "MEMBER_OF": (frozenset({"Person"}), frozenset({"Team"})),
    "DEPENDS_ON": (frozenset({"Service", "Customer"}), frozenset({"Service", "Datastore"})),
    "CAUSED_BY": (frozenset({"Incident"}), frozenset({"Service", "Datastore"})),
    "IMPACTS": (frozenset({"Incident", "Service"}), frozenset({"Customer", "Service"})),
    "MITIGATED_BY": (frozenset({"Incident"}), frozenset({"Runbook"})),
    "ONCALL_FOR": (frozenset({"Person"}), frozenset({"Service"})),
    "DOCUMENTED_IN": (
        frozenset({"Incident", "Service", "Datastore"}),
        frozenset({"Runbook"}),
    ),
}


def _signature_error(t: "RawTriple") -> str | None:
    """Return a reason string if the triple violates its relation's signature."""
    sig = _RELATION_SIGNATURES.get(t.predicate)
    if sig is None:
        return None  # unknown predicate is handled by the closed-world check
    subjects, objects = sig
    if t.subject_type not in subjects:
        return (
            f"{t.predicate} subject must be {'/'.join(sorted(subjects))}, "
            f"got {t.subject_type}"
        )
    if t.object_type not in objects:
        return (
            f"{t.predicate} object must be {'/'.join(sorted(objects))}, "
            f"got {t.object_type}"
        )
    return None


_PROMPT = """Extract a knowledge graph from the document below.

Allowed entity types (use EXACTLY these, nothing else):
  {entity_types}
Allowed relation types, with REQUIRED direction (subject -RELATION-> object):
  Person|Team OWNS Service|Datastore          (the owner is the SUBJECT)
  Person MEMBER_OF Team
  Person ONCALL_FOR Service                    (the person is the SUBJECT)
  Service|Customer DEPENDS_ON Service|Datastore
  Incident CAUSED_BY Service|Datastore         (the incident is the SUBJECT)
  Incident|Service IMPACTS Customer|Service    (the impacted party is the OBJECT)
  Incident MITIGATED_BY Runbook                (the incident is the SUBJECT)
  Incident|Service|Datastore DOCUMENTED_IN Runbook

Rules:
- Only extract facts stated in the document. Do not infer or invent.
- NEVER add transitive shortcuts. If the document says A depends on B and
  B depends on C, do NOT emit "A DEPENDS_ON C" — a narrative like "if C is
  degraded, B fails, then A fails" describes propagation through the chain,
  not a direct dependency. Emit only the directly stated edges.
- Get the direction right. "Root cause was X" about incident I means
  I CAUSED_BY X — never X CAUSED_BY I. "Followed runbook R" during incident I
  means I MITIGATED_BY R.
- Name entities by their short canonical name as the document refers to them
  (e.g. the runbook called payments-latency is named "payments-latency";
  the incident is "INC-1042"). Never invent generic-concept entities
  ("payment errors" or "charge flows" are not entities).
- Person entities are NAMED individuals only. A role or rotation ("the
  payments-api on-call", "the secondary on-call") is not an entity — skip it
  unless the document names the person holding it.
- Keep an entity's type consistent with what it is: a database like ledger-db
  is a Datastore, never a Service.
- Every triple's subject and object must also appear in `entities`.

Document ({source_path}):
{text}"""


def _default_generate(prompt: str) -> RawDelta:
    """Real Gemini call, same shape as docsbot.extract.summarize."""
    from google.genai import types

    from ..client import get_client
    from ..config import settings

    response = get_client().models.generate_content(
        model=settings.extract_model,  # cheaper model: extraction is bulk work
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RawDelta,
            temperature=0.0,  # facts should not vary run to run
        ),
    )
    return response.parsed  # type: ignore[return-value]


def extract_graph_delta(
    text: str,
    *,
    source_path: str,
    content_hash: str,
    generate: Callable[..., Any] | None = None,
    extracted_at: datetime | None = None,
) -> GraphDelta:
    """Extract entities + triples from one document.

    Parameters
    ----------
    text:
        Raw markdown/plaintext of the source file.
    source_path:
        Repo-relative path recorded on every triple's provenance.
    content_hash:
        Hash of `text` (sha256 hex). Must match what ingest computed.
    generate:
        Optional injectable LLM callable for tests. If None, use the real
        Gemini client (same spirit as docsbot.extract.summarize).
    extracted_at:
        Override timestamp (tests). Default: utcnow.
    """
    prompt = _PROMPT.format(
        entity_types=", ".join(sorted(ENTITY_TYPES)),
        relation_types=", ".join(sorted(RELATION_TYPES)),
        source_path=source_path,
        text=text,
    )
    raw = (generate or _default_generate)(prompt)
    if isinstance(raw, dict):
        raw = RawDelta.model_validate(raw)
    elif isinstance(raw, str):
        raw = RawDelta.model_validate_json(raw)
    if raw is None:
        raise ValueError(f"extraction returned nothing for {source_path}")

    prov = Provenance(
        source_path=source_path,
        content_hash=content_hash,
        extracted_at=extracted_at or utcnow(),
    )
    delta = GraphDelta()
    known_ids: set[str] = set()

    # Step 1+2: validate each row against the closed world; quarantine strays
    # (one hallucinated type must not sink the whole document).
    for ent in raw.entities:
        if ent.type not in ENTITY_TYPES:
            delta.quarantined.append(
                f"{source_path}: unknown entity type {ent.type!r} for {ent.name!r}"
            )
            continue
        try:
            eid = entity_id(ent.type, ent.name)
        except ValueError as err:
            delta.quarantined.append(f"{source_path}: {err}")
            continue
        if eid not in known_ids:
            known_ids.add(eid)
            delta.entities.append(Entity(type=ent.type, name=ent.name))

    for t in raw.triples:
        if t.predicate not in RELATION_TYPES:
            delta.quarantined.append(
                f"{source_path}: unknown relation {t.predicate!r} "
                f"({t.subject_name} → {t.object_name})"
            )
            continue
        if t.subject_type not in ENTITY_TYPES or t.object_type not in ENTITY_TYPES:
            delta.quarantined.append(
                f"{source_path}: triple with unknown endpoint type "
                f"({t.subject_type!r} → {t.object_type!r})"
            )
            continue
        sig_err = _signature_error(t)
        if sig_err is not None:
            delta.quarantined.append(
                f"{source_path}: {sig_err} "
                f"({t.subject_name} -{t.predicate}-> {t.object_name})"
            )
            continue
        try:
            sid = entity_id(t.subject_type, t.subject_name)
            oid = entity_id(t.object_type, t.object_name)
        except ValueError as err:
            delta.quarantined.append(f"{source_path}: {err}")
            continue
        # The model was told every endpoint must be declared in entities; if it
        # forgot, repair rather than quarantine — the fact itself is usable.
        for etype, name, eid in ((t.subject_type, t.subject_name, sid),
                                 (t.object_type, t.object_name, oid)):
            if eid not in known_ids:
                known_ids.add(eid)
                delta.entities.append(Entity(type=etype, name=name))
        delta.triples.append(
            Triple(subject_id=sid, predicate=t.predicate, object_id=oid, provenance=prov)
        )

    _prune_transitive_shortcuts(delta, source_path)
    return delta


# Relations where A→B→C plus a direct A→C is (almost) always the model
# compressing a narrative chain, not a documented direct edge.
_REDUCIBLE = frozenset({"DEPENDS_ON"})


def _prune_transitive_shortcuts(delta: GraphDelta, source_path: str) -> None:
    """Transitive reduction per document: quarantine A→C when A→B and B→C exist.

    The prompt forbids these shortcuts, but temperature 0 is not determinism —
    the model still occasionally compresses "if C degrades, B fails, then A
    fails" into a direct A→C edge. A deterministic reduction pass is graph
    hygiene the prompt cannot guarantee; the schema layer can.
    """
    for pred in _REDUCIBLE:
        edges = {
            (t.subject_id, t.object_id) for t in delta.triples if t.predicate == pred
        }
        outs: dict[str, set[str]] = {}
        for a, b in edges:
            outs.setdefault(a, set()).add(b)
        shortcuts = {
            (a, c)
            for a, c in edges
            # b must be a genuine intermediate: a self-loop A->A would otherwise
            # make every real edge A->X look like a "shortcut" via A->A->X.
            if a != c
            and any(c in outs.get(b, ()) for b in outs.get(a, ()) if b != c and b != a)
        }
        if not shortcuts:
            continue
        kept: list[Triple] = []
        for t in delta.triples:
            if t.predicate == pred and (t.subject_id, t.object_id) in shortcuts:
                delta.quarantined.append(
                    f"{source_path}: transitive shortcut pruned "
                    f"({t.subject_id} -{pred}-> {t.object_id})"
                )
            else:
                kept.append(t)
        delta.triples[:] = kept


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
