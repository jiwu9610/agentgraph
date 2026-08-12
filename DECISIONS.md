# Design decisions and findings — knowledge-graph agent

Working log for the knowledge-graph agent track.
One entry per non-obvious choice, with the why.

## Knowledge graph

- **Closed-world check validates values, not pydantic.** `assert_closed_world_delta`
  re-checks every type against the frozen ontology because rows built via
  `model_construct` (or loaded from an old DB) bypass pydantic validators entirely —
  the guard exists precisely for the rows validation never saw.
- **Multi-provenance via `UNIQUE(subject, predicate, object, source_path)`.** The same
  fact from the same file is idempotent on re-ingest; the same fact from a *different*
  file is a second row. That's what lets an answer cite every document asserting a fact.
- **Content hash is the change detector** (same trick as the vector store's content-hash ingest): unchanged
  file → zero LLM calls on re-ingest. Quarantined files deliberately do NOT record their
  hash, so the next run retries them instead of silently marking them done.
- **Soft delete removes edges, keeps entities.** A dangling entity is harmless; a dangling
  edge is a wrong answer. The document row is dropped too, so a file that reappears
  re-extracts rather than being skipped on a stale hash match.

## Traversal agent

- **Two stop conditions, deliberately different.** `BudgetExceeded` PROPAGATES (a budget
  is the caller's authority limit — hiding a breach in a polite answer means nobody learns
  the ceiling was hit). A FUTILE LOOP degrades gracefully: a repeated identical tool call
  that adds no new edges twice in a row → the agent refuses on its own. The two specs
  (phase14 cycle test vs phase16 trap test) are only satisfiable with this split.
- **Grounding gate:** a positive answer with an empty path is converted to a refusal.
  Graph version of RAG's "not in the docs" — no citable trail, no claim.
- **A hop = the frontier actually grew.** Expansion calls that return nothing new don't
  burn hop budget; they feed the futility counter instead.
- **`lookup` is substring, not exact** — the agent must SEE ambiguity ("Mira" surfaces
  both "Mira Chen" and "Mira Chen Bot") rather than silently picking one. Exact matches
  sort first. LIKE wildcards in names are escaped.

## Sessions

- **Checkpoint = one JSON blob keyed by session.** Its shape evolves with the agent;
  a blob turns schema evolution into a dataclass edit instead of a migration.
  `completed_tool_call_ids` + stored results are what make resume skip re-execution.
- **Compaction keeps the distilled state** (visited ids, path), drops bulky tool payloads.

## Budgets

- **Check BEFORE the call, record after.** Production systems that check after the fact
  only write nicer postmortems (the module docstring's line — it's correct).

## Tracing

- **One redaction choke point.** Every span attribute passes through `redact_value` inside
  the span helper, so instrumented call sites are honest by construction, not by review.
  Secret-looking keys (`api_key`, `token`, `authorization`, ...) → `[REDACTED]`;
  huge strings truncated.
- **Injected provider = same code path as real Phoenix.** Instrumented code can't tell
  a test fake from production wiring; that's what makes spans testable offline.

## Live end-to-end — what the specs could NOT catch

All 28 Part-3 specs were green before the first live run scored **2/7** on the KG evals.
Fakes prove your logic; they cannot prove your prompts. Every fix below came from
diagnosing real Gemini behavior:

1. **Runner bug found only live:** multi-provenance duplicates within ONE tool result
   slipped past dedup (the seen-set was updated after batch filtering, not during).
2. **Extraction direction errors** (flash-lite): `Runbook MITIGATED_BY Datastore`
   (backwards), `Service ONCALL_FOR Runbook`. Fix: relation SIGNATURES in the prompt
   (subject/object types per relation) plus a schema-side guardrail that quarantines
   violating triples — the model can drift; the schema can't.
3. **Transitive closure hallucination:** "if ledger-db degrades, ledger-service fails,
   then payments-api fails" became a direct `payments-api DEPENDS_ON ledger-db` edge.
   Fix: explicit "never add transitive shortcuts" rule. This one matters: a graph with
   invented shortcuts gives RIGHT answers with WRONG citations.
4. **Phantom role entities:** "page the payments-api on-call" produced
   `Person:payments-api-on-call`, which then made the agent refuse a real question as
   ambiguous. Fix: Person = named individuals only.
5. **Slug leakage in answers:** the agent said `mira-chen` where the eval (rightly)
   demands the display name "Mira Chen". Fix: display-name rule in the system prompt.
6. **Case-sensitive tool inputs:** the model calls `follow_edge(predicate="owns")`;
   the store speaks `OWNS`. Fix: normalize enums at the tool boundary.
7. **Nondeterminism:** the same eval case passed or failed across runs at default
   temperature. Fix: `temperature=0` for BOTH the extractor and the agent driver —
   and measure stability across repeated runs rather than trusting any single one.
8. **Model escalation as a config knob:** extraction moved from flash-lite to flash via
   `DOCSBOT_EXTRACT_MODEL` (the env override that exists for exactly this)
   after lite kept fumbling relation direction. Bulk-cheap is the right default;
   direction fidelity is worth paying for.
9. **Agent strategy gaps only show up deterministic.** Once temp-0 made runs
   reproducible, the flaky failures separated into two *strategy* bugs: (a) diagnosis
   questions never consulted Incident history — the graph documents propagation
   (`inc-1042 IMPACTS brightline`, `inc-1042 CAUSED_BY ledger-db`) but the agent
   answered from adjacency, picking the nearest Datastore instead of the documented
   culprit; (b) two valid candidates (primary + secondary on-call) sometimes read as
   "ambiguous → refuse". Both fixed as explicit strategy rules: check incident
   history on diagnosis questions; ambiguity between valid candidates is an answer,
   not a refusal.
10. **Measure stability, not single runs.** The eval is scored as N repeated runs on
    frozen code (a mid-measurement code edit invalidates the batch — learned that the
    embarrassing way). A case that passes 1-of-3 is a bug you haven't found yet, not
    a pass.
11. **Temperature 0 is not determinism.** With temp 0 on the extractor, identical
    ingests still occasionally re-emitted the forbidden transitive-shortcut edges the
    prompt rules against. Prompts SUPPRESS failure modes; they don't eliminate them.
    The fix that actually holds is mechanical: a **transitive reduction** pass over
    each extracted delta (A→C quarantined when A→B and B→C exist for DEPENDS_ON) —
    deterministic graph hygiene at the schema layer, where guarantees live.
12. **Honest caveat on the eval loop:** the agent's strategy rules were developed
    against the golden set (the eval defines the target), but
    generalization would need held-out questions the prompt was never tuned on.

## Final numbers (2026-07-25)

- Specs: **28/28** for the agent track, **83/83** across the repo at that date (67 Python + 16 frontend).
- Live KG eval: **2/7 → 6/7, stable across 3 frozen-code runs** (and the 3 before).
- Sample multi-hop query (INC-1042 root cause + owner): correct answer with a
  2-hop citation path, **4 LLM calls / 3 tool calls / 3,589 in + 114 out tokens /
  $0.000303** against the $0.05 budget cap.

## Known limitation (deliberately left open)

`brightline-dependency-walk` fails 6/6 stable runs, from two compounding causes:

1. **Cross-document transitive shortcuts survive.** The per-delta transitive
   reduction can't see a shortcut whose intermediate hops were extracted from a
   *different* document. Fix shape: run the same reduction at ingest time against
   the whole store graph, not just the incoming delta.
2. **The agent under-explores.** Even with explicit walk-completion and
   incident-history rules, flash answers with the *adjacent* Datastore
   (payments-cache) instead of walking one more hop to the chain's documented
   culprit (ledger-db), and never tries the incident route. Prompt rules suppress
   but don't force behavior; a mechanical fix would gate finalization on "every
   discovered Service expanded" in the runner itself, mirroring how the grounding
   gate works.

Left open on purpose: the diagnosis is complete, the fix shapes are named, and the
remaining work is engineering, not mystery.

## Robustness pass (2026-08-03) — bugs the specs never exercised

Adversarial re-review of the agent loop found three real defects, all in the
"model sends something malformed" category no spec covers. Each was reproduced,
fixed, and re-verified (specs stayed 28/28):

1. **Span-attribute collision:** a model-supplied tool arg literally named
   `hop_index` crashed `span_tool` with a duplicate-kwarg TypeError. Fix: namespace
   model args (`arg.entity_id`) before they reach the span — untrusted keys never
   meet a Python signature.
2. **Malformed tool call crashed the whole turn:** a typo'd arg name
   (`{"nam": "Mira"}`) raised TypeError through `dispatch_tool` and killed the
   agent. Fix: feed `{"error": ...}` back to the model as the tool result so it can
   self-correct; the futility guard still stops repeat offenders; `BudgetExceeded`
   deliberately NOT caught — authority breaches must propagate.
3. **Checkpoint status lied:** the catch-all labeled ANY crash `budget_exceeded`.
   Fix: only `BudgetExceeded` earns that label; unexpected crashes keep the
   checkpoint `running` so a resume can retry honestly.

## Adversarial review round 2 (2026-08-03) — 24 raised, 18 confirmed, 13 fixed

A 5-lens refute-first review (agent loop, resume semantics, SQL, extraction,
tracing/tools) confirmed 18 defects beyond the specs' reach. Fixed the same day
(all specs stayed green; live eval stayed 6/7):

- **Resume deadlock (the big one):** the default driver's tool-call ids restarted
  per process, so resumed calls collided with completed ids and were silently
  skipped; at temperature 0 the identical prompt re-issued the same decision until
  the LLM budget burned — permanent deadlock once completed calls ≥ max_llm_calls.
  Fixed threefold: uuid ids (unique across lives), the futility guard now also
  covers the cache-hit path, and `executed_calls` seeds from the checkpoint.
- Check-before actually estimating the next call's tokens, not just auditing past
  spend; compacted results re-execute instead of replaying as `None`; checkpoints
  from unknown schemas load tolerantly; compaction now also prunes tool messages.
- A missing/typo'd corpus dir now raises loudly instead of the deletion sweep
  silently wiping every triple; one broken document quarantines instead of
  aborting the whole ingest; `upsert_delta` is transactional (all-or-nothing).
- Empty-name lookup no longer returns the entire table; relaxed lookup matches
  are marked `"match": "relaxed"` so the model can weigh them; self-loops can't
  fool transitive reduction; non-string model args degrade instead of raising.

Documented, not fixed (deliberate tradeoffs): transitive reduction can quarantine
a genuinely-stated direct edge that parallels a chain (precision over recall);
resuming with a different question inherits the old path; `append_message` can
race across two connections (single-user scope).
