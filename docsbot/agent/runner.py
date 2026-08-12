"""Budgeted graph traversal agent.

Pseudocode of a correct runner:

    checkpoint = load or new
    while not done:
        budget.check_llm(...)
        decision = llm(messages, tools)          # may be injected
        if decision is final answer:
            verify path non-empty OR refuse
            return AgentAnswer(...)
        budget.check_tool(); budget.check_hop()
        if tool_call.id in checkpoint.completed_tool_call_ids:
            reuse prior result                    # resume safety
        else:
            result = dispatch(tool_call)
            record + append path edges if any
        save_checkpoint(...)

Refuse when the graph cannot support the claim. Never invent edges.

Two stop conditions worth telling apart:
  - BudgetExceeded PROPAGATES. A budget is the caller's authority limit; hiding
    a breach inside a polite answer means nobody learns the ceiling was hit.
  - A FUTILE LOOP degrades gracefully. If the model repeats a tool call it has
    already made and learns nothing new twice in a row, the agent refuses on its
    own — that's an agent-quality problem, not an authority problem.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from docsbot.agent import tools as tools_mod
from docsbot.agent.budget import Budget, BudgetExceeded
from docsbot.agent.state import AgentCheckpoint, SessionStore
from docsbot.kg.store import KnowledgeGraphStore

# Tool calls that expand the graph frontier count as hops; lookups don't.
_EXPANDING_TOOLS = frozenset({"get_neighbors", "follow_edge"})

_TOOLS: dict[str, Callable[..., Any]] = {
    "lookup_entity": tools_mod.lookup_entity,
    "get_entity": tools_mod.get_entity,
    "get_neighbors": tools_mod.get_neighbors,
    "follow_edge": tools_mod.follow_edge,
}


@dataclass
class PathHop:
    subject_id: str
    predicate: str
    object_id: str


@dataclass
class AgentAnswer:
    text: str
    path: list[PathHop] = field(default_factory=list)
    refused: bool = False
    session_id: str | None = None
    budget_snapshot: dict[str, Any] = field(default_factory=dict)


def dispatch_tool(
    name: str,
    args: dict[str, Any],
    store: KnowledgeGraphStore,
) -> Any:
    """Map a tool name to docsbot.agent.tools.* — used by the runner + tests."""
    fn = _TOOLS.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name!r} (allowed: {sorted(_TOOLS)})")
    return fn(store, **args)


def _hops_from_result(name: str, args: dict[str, Any], result: Any) -> list[PathHop]:
    """Turn a tool result into path hops (the citable trail)."""
    hops: list[PathHop] = []
    if name == "get_neighbors" and isinstance(result, list):
        for row in result:
            hops.append(PathHop(row["subject_id"], row["predicate"], row["object_id"]))
    elif name == "follow_edge" and isinstance(result, list):
        # Normalize the predicate the same way the tool did, so the citable
        # path never carries a casing that doesn't exist in the graph.
        pred = str(args.get("predicate", "")).strip().upper()
        for ent in result:
            hops.append(PathHop(args["entity_id"], pred, ent["id"]))
    return hops


def _entity_ids_in(result: Any) -> list[str]:
    ids: list[str] = []
    if isinstance(result, list):
        for row in result:
            if isinstance(row, dict):
                for key in ("id", "subject_id", "object_id"):
                    if key in row:
                        ids.append(row[key])
    elif isinstance(result, dict) and "id" in result:
        ids.append(result["id"])
    return ids


def run_agent(
    question: str,
    store: KnowledgeGraphStore,
    *,
    budget: Budget | None = None,
    session_store: SessionStore | None = None,
    session_id: str | None = None,
    user_id: str = "default",
    llm: Callable[..., Any] | None = None,
    resume: bool = False,
) -> AgentAnswer:
    """Run (or resume) one agent turn over the knowledge graph."""
    budget = budget or Budget()
    if llm is None:
        llm = _build_default_llm(store)

    # --- session / checkpoint bootstrap -----------------------------------
    cp: AgentCheckpoint | None = None
    if session_store is not None:
        if session_id is None:
            session_id = session_store.create_session(user_id)
        if resume:
            cp = session_store.load_checkpoint(session_id)
    if cp is None:
        # Fresh checkpoint (including resume=True with nothing to resume): the
        # user message is recorded whenever a new conversation actually starts.
        cp = AgentCheckpoint(session_id=session_id or "", question=question)
        if session_store is not None and session_id is not None:
            session_store.append_message(session_id, "user", question)

    path: list[PathHop] = [PathHop(**h) for h in cp.path]
    seen_hops: set[tuple[str, str, str]] = {
        (h.subject_id, h.predicate, h.object_id) for h in path
    }
    completed: set[str] = set(cp.completed_tool_call_ids)
    results_by_id: dict[str, dict[str, Any]] = {r["id"]: r for r in cp.tool_results}
    visited: set[str] = set(cp.visited_entity_ids)
    if not cp.messages:
        cp.messages = [{"role": "user", "content": question}]

    # Seed from the checkpoint so the futility guard survives a restart —
    # otherwise a resumed agent can re-tread old ground with a fresh streak.
    executed_calls: set[str] = {
        f"{r['name']}:{json.dumps(r.get('args', {}), sort_keys=True)}"
        for r in cp.tool_results
    }
    futile_streak = 0

    def _save(status: str = "running") -> None:
        if session_store is None or session_id is None:
            return
        cp.status = status
        cp.path = [vars(h) for h in path]
        cp.visited_entity_ids = sorted(visited)
        cp.completed_tool_call_ids = sorted(completed)
        session_store.save_checkpoint(cp)

    def _finish(text: str, refused: bool) -> AgentAnswer:
        _save("refused" if refused else "answered")
        if session_store is not None and session_id is not None:
            session_store.append_message(session_id, "assistant", text)
        return AgentAnswer(
            text=text,
            path=list(path),
            refused=refused,
            session_id=session_id,
            budget_snapshot=budget.remaining(),
        )

    from docsbot.tracing.instrumentation import span_agent_turn, span_llm, span_tool

    try:
        with span_agent_turn(session_id or "", question):
          while True:
            # Check-BEFORE means estimating the call we're about to make, not
            # just auditing the ones already made (~4 chars/token heuristic).
            est_tokens = (
                len(question) + len(json.dumps(cp.tool_results, default=str))
            ) // 4 + 256
            budget.check_llm(estimated_input_tokens=est_tokens)
            with span_llm(model="agent-driver"):
                decision = llm(
                    question=question,
                    messages=cp.messages,
                    tool_results=cp.tool_results,
                    path=[vars(h) for h in path],
                )
            budget.record_llm(
                input_tokens=int(decision.get("input_tokens", 0)),
                output_tokens=int(decision.get("output_tokens", 0)),
            )

            if decision["type"] == "final":
                refused = bool(decision.get("refused", False))
                # Grounding gate: a positive claim with no supporting path is
                # exactly the failure RAG refused with "not in the docs" — the
                # graph version refuses when there is no citable edge trail.
                if not refused and not path:
                    return _finish(
                        "I can't support an answer from the knowledge graph "
                        "(no supporting path was found).",
                        refused=True,
                    )
                return _finish(decision.get("text", ""), refused=refused)

            # --- tool decision ------------------------------------------------
            call_id = str(decision.get("id", f"call{len(completed)}"))
            name = decision["name"]
            args = dict(decision.get("args", {}))

            signature = f"{name}:{json.dumps(args, sort_keys=True)}"
            if call_id in completed and call_id in results_by_id:
                # Resume safety: this call already ran in a previous life —
                # reuse its recorded result, spend nothing. (A completed id whose
                # result was compacted away falls through and re-executes.)
                budget.record_cache_hit()
                # The futility guard must also live on this path, or a driver
                # that replays ids spins here forever burning LLM budget.
                if signature in executed_calls:
                    futile_streak += 1
                    if futile_streak >= 2:
                        return _finish(
                            "I stopped: the traversal was revisiting the same "
                            "edges without making progress.",
                            refused=True,
                        )
                executed_calls.add(signature)
            else:
                budget.check_tool()
                if name in _EXPANDING_TOOLS:
                    budget.check_hop()
                # Model-supplied args are namespaced ("arg.entity_id") so a key
                # like "hop_index" can't collide with span_tool's own parameters.
                span_attrs = {f"arg.{k}": v for k, v in args.items()}
                try:
                    with span_tool(name, hop_index=budget.hops, **span_attrs):
                        result = dispatch_tool(name, args, store)
                except (TypeError, ValueError, AttributeError) as err:
                    # A malformed call (typo'd arg, unknown tool) is the MODEL's
                    # mistake — feed it back as a result so it can self-correct;
                    # the futility guard stops it if it keeps repeating. Budget
                    # breaches are NOT caught here: they must propagate.
                    result = {"error": f"{type(err).__name__}: {err}"}
                budget.record_tool()

                new_hops: list[PathHop] = []
                for h in _hops_from_result(name, args, result):
                    key = (h.subject_id, h.predicate, h.object_id)
                    if key in seen_hops:
                        continue  # multi-provenance: same fact, second source
                    seen_hops.add(key)  # add as we go — one result can repeat a fact
                    new_hops.append(h)
                    path.append(h)
                if name in _EXPANDING_TOOLS and new_hops:
                    budget.record_hop()  # a hop = the frontier actually grew
                visited.update(_entity_ids_in(result))

                completed.add(call_id)
                record = {"id": call_id, "name": name, "args": args, "result": result}
                cp.tool_results.append(record)
                results_by_id[call_id] = record
                cp.messages.append(
                    {"role": "tool", "id": call_id, "name": name,
                     "content": json.dumps(result, default=str)}
                )

                # Futility guard: an identical repeat that taught us nothing.
                if signature in executed_calls and not new_hops:
                    futile_streak += 1
                    if futile_streak >= 2:
                        return _finish(
                            "I stopped: the traversal was revisiting the same "
                            "edges without making progress.",
                            refused=True,
                        )
                else:
                    futile_streak = 0
                executed_calls.add(signature)

            _save("running")
    except BudgetExceeded:
        _save("budget_exceeded")
        raise
    except Exception:
        # An unexpected crash is not a budget event: keep the checkpoint
        # "running" so a resume can pick up where it left off.
        _save("running")
        raise


def _build_default_llm(store: KnowledgeGraphStore) -> Callable[..., Any]:
    """Real-LLM driver: Gemini function calling, adapted to the decision dicts
    the runner speaks. Manual loop (no automatic function calling) so budgets
    and checkpoints see every step."""
    from google.genai import types

    from docsbot.client import get_client
    from docsbot.config import settings

    system = (
        "You answer questions using ONLY a knowledge graph, by calling tools.\n"
        "Procedure:\n"
        "1. lookup_entity for each entity the question names.\n"
        "2. Expand with get_neighbors. For 'who owns/is on call for X' ask "
        "direction='in' on X (the owner points AT it). Predicates are "
        "UPPERCASE: OWNS, MEMBER_OF, DEPENDS_ON, CAUSED_BY, IMPACTS, "
        "MITIGATED_BY, ONCALL_FOR, DOCUMENTED_IN.\n"
        "3. For diagnosis questions ('X is failing, what should we inspect'), "
        "check incident history FIRST, before any dependency reasoning: "
        "get_neighbors(direction='in') on the affected entity reveals an "
        "Incident that IMPACTS it; expand that Incident — its CAUSED_BY edge "
        "names the documented culprit. An incident-corroborated culprit "
        "always beats a merely-adjacent candidate.\n"
        "4. For chain questions ('...through Y'), walk hop by hop with "
        "get_neighbors(direction='out', predicate='DEPENDS_ON') on each "
        "discovered node. Finding SOME entity of the asked type does NOT end "
        "the walk while the chain continues through further Services — "
        "follow it to the entity the chain (or the incident record) actually "
        "implicates.\n"
        "5. Answer AS SOON AS the tool transcript contains the supporting "
        "edges — do not keep exploring, and never repeat a call you already "
        "made (the transcript below shows every call so far).\n"
        "Rules: never invent entities or edges. In the final answer refer to "
        "every entity on the supporting path by its display NAME exactly as "
        "stored ('Mira Chen', 'ledger-db') — never ids or slugs like "
        "Person:mira-chen or mira-chen. If a lookup misses, retry with "
        "shorter sub-phrases of the name before concluding it is absent. "
        "Multiple valid candidates (e.g. two people on call) is NOT a reason "
        "to refuse — answer with all of them, most specific/primary first. "
        "Refuse ONLY if lookup found nothing or the needed edges are absent "
        "after expanding — then reply with exactly: REFUSED: <one-line reason>. "
        "FINAL CHECK before answering a 'which upstream/root X' question: you "
        "must have expanded EVERY Service on the dependency chain (or found an "
        "Incident's CAUSED_BY). If any discovered Service is still unexpanded, "
        "expand it before answering."
    )
    gemini_tools = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(**d) for d in tools_mod.TOOL_DECLARATIONS
        ]
    )
    import uuid  # ids must be unique ACROSS process lives, or resume replays
    # collide with completed_tool_call_ids and new calls get skipped as cache
    # hits — the resume-deadlock found in adversarial review.

    def _llm(question: str, tool_results: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        # Stateless per call: re-send everything.
        transcript = "\n".join(
            f"[tool {r['name']}({json.dumps(r.get('args', {}))}) -> "
            f"{json.dumps(r['result'], default=str)[:2000]}]"
            for r in tool_results
        )
        prompt = f"Question: {question}\n\nTool calls so far:\n{transcript or '(none)'}"
        response = get_client().models.generate_content(
            model=settings.chat_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=[gemini_tools],
                temperature=0.0,  # traversal decisions should be reproducible
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        usage = getattr(response, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0

        calls = response.function_calls or []
        if calls:
            fc = calls[0]
            return {
                "type": "tool",
                "id": f"fc-{uuid.uuid4().hex[:8]}",
                "name": fc.name,
                "args": dict(fc.args or {}),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            }
        text = (response.text or "").strip()
        refused = text.startswith("REFUSED:")
        return {
            "type": "final",
            "text": text.removeprefix("REFUSED:").strip() if refused else text,
            "refused": refused,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        }

    return _llm
