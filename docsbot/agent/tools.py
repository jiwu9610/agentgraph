"""Graph tools the agent may call.

Each tool is a plain function with a JSON-serializable result. The runner
registers these with Gemini function-calling (or a fake in tests).

Rules:
  - Never invent entities. If lookup misses, return [].
  - lookup may return multiple hits — the agent must disambiguate.
  - follow_edge / neighbors are the only way to expand the frontier.
"""

from __future__ import annotations

from typing import Any

from docsbot.kg.store import KnowledgeGraphStore


def _entity_dict(e) -> dict[str, Any]:
    return {"id": e.id, "type": e.type, "name": e.name, "attrs": e.attrs}


def lookup_entity(
    store: KnowledgeGraphStore,
    name: str,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return matching entities as dicts: id, type, name, attrs."""
    hits = store.lookup(name, entity_type)
    if not hits and entity_type is not None:
        # The model sometimes guesses the wrong type; a typed miss shouldn't
        # read as "entity doesn't exist" when an untyped hit would find it.
        hits = store.lookup(name)
    if not hits and " " in name.strip():
        # Progressive relaxation: "Brightline Retail checkout" matches nothing
        # as a phrase, but its tokens surface Customer:brightline-retail and
        # Service:checkout-web — the ambiguity is the agent's to resolve.
        # Relaxed hits are MARKED so the model can tell "found it" apart from
        # "found something vaguely related" when deciding whether to refuse.
        seen: set[str] = set()
        relaxed: list[dict[str, Any]] = []
        for token in name.split():
            if len(token) < 4:
                continue  # too short to be a discriminating name fragment
            for e in store.lookup(token):
                if e.id not in seen:
                    seen.add(e.id)
                    relaxed.append({**_entity_dict(e), "match": "relaxed"})
        return relaxed
    return [_entity_dict(e) for e in hits]


def get_entity(store: KnowledgeGraphStore, entity_id: str) -> dict[str, Any] | None:
    e = store.get_entity(entity_id)
    return _entity_dict(e) if e is not None else None


def get_neighbors(
    store: KnowledgeGraphStore,
    entity_id: str,
    predicate: str | None = None,
    direction: str = "out",
) -> list[dict[str, Any]]:
    """Return triples as dicts including subject_id, predicate, object_id."""
    # Normalize model-supplied enums: "owns" and "OWNS" mean the same edge.
    # isinstance guards because these values come from the MODEL — a null or
    # non-string arg must degrade, not raise AttributeError mid-turn.
    predicate = predicate.strip().upper() if isinstance(predicate, str) and predicate else None
    direction = direction.strip().lower() if isinstance(direction, str) and direction else "out"
    return [
        {
            "subject_id": t.subject_id,
            "predicate": t.predicate,
            "object_id": t.object_id,
            "source_path": t.provenance.source_path,  # keeps hops citable
        }
        for t in store.neighbors(entity_id, predicate=predicate, direction=direction)
    ]


def follow_edge(
    store: KnowledgeGraphStore,
    entity_id: str,
    predicate: str,
) -> list[dict[str, Any]]:
    """Outbound follow; return entity dicts."""
    return [
        _entity_dict(e)
        for e in store.follow_edge(entity_id, str(predicate).strip().upper())
    ]


# Gemini-compatible function schemas, used by the runner's real-LLM path.
TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "lookup_entity",
        "description": (
            "Find entities by (partial) name, optionally filtered by type. "
            "Returns [] if nothing matches — never invent entities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "entity_type": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_entity",
        "description": "Fetch one entity by its exact id (e.g. 'Service:payments-api').",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "get_neighbors",
        "description": (
            "List edges touching an entity. direction is 'out', 'in', or 'both'; "
            "optionally filter by predicate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "predicate": {"type": "string"},
                "direction": {"type": "string"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "follow_edge",
        "description": "Follow an outbound predicate from an entity; returns the target entities.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "predicate": {"type": "string"},
            },
            "required": ["entity_id", "predicate"],
        },
    },
]
