# DocsBot — Phase Three: knowledge-graph agent in production

Phase one got you a working RAG bot. Phase two turned it into a product
(persist, retries, hybrid retrieval, API, UI). **Phase three is the real job:**
you operate over a **structured knowledge graph**, answer with a **stateful
multi-hop agent**, stay **inside a cost budget**, and **debug from Phoenix
traces** when something goes wrong.

This is how production AI systems behave when chunk RAG is not enough —
ownership questions, dependency walks, incident triage, "who / what / depends
on" — need **entities and edges**, not nearest neighbors alone.

```
13 Graph     → 14 Traversal agent → 15 Stateful sessions → 16 Cost gates → 17 Phoenix + ops
   ontology,      tools, hops,         checkpoints,            budgets,        traces, evals,
   extract,       path citations,      resume-after-crash,     caches,         break/fix drills
   SQLite KG      refuse-if-unknown    working memory          early stop
```

## How phase three is different

Same contract as phase two: **stubs + failing tests are the spec.** No
reference implementation on `part3`. Make `pytest -m phase13` (etc.) green.

Phase three also ships a **realistic corpus** — Northstar Cloud, a fictional
payments SaaS — with org charts, services, dependencies, incidents, and
runbooks. Multi-hop questions are the point. If your agent answers from vibes
instead of graph paths, the evals fail.

> Prerequisite: Parts 1–2 complete (or work on `part3`, which includes the
> Part 2 reference implementation). You will reuse retries, cost ledger ideas,
> and the FastAPI surface.

### Running the specs

```bash
pip install -e ".[dev,kg,obs]"   # SQLite is stdlib; obs = Phoenix/OTel
pytest                           # Parts 1 (+2 if present) stay green
pytest -m phase13                # KG schema + store + ingest
pytest -m phase14                # traversal agent
pytest -m phase15                # durable sessions / checkpoints
pytest -m phase16                # budgets + caches
pytest -m phase17                # Phoenix instrumentation contracts
pytest -m phase3                 # every phase-three spec
```

Most specs need **no API key** — they inject fakes for the LLM and Phoenix
exporter so you test *your* logic. Live drills (end of Phase 17) need a key and a
local Phoenix (`docker compose --profile obs up`).

---

## Phase 13 — Knowledge graph foundation · `docsbot/kg/`

**Goal:** turn messy docs into a **typed, durable, idempotent** knowledge graph.

**You build:**
- `schema.py` — closed ontology (`EntityType`, `RelationType`), `Entity`,
  `Triple`, `GraphDelta`, deterministic `entity_id(type, name)`
- `extract.py` — LLM → structured `GraphDelta` (Pydantic). Reject unknown types.
- `store.py` — SQLite graph: entities, triples, provenance, content hashes
- `ingest.py` — walk `corpus/northstar/`, hash each file, skip unchanged,
  upsert deltas, soft-delete triples whose source doc vanished
- `validate.py` — reject orphan edges; report conflicts; quarantine bad deltas

**Real-world constraints the tests enforce:**
- Closed world: unknown entity/relation types → hard error (or quarantine),
  never silently invent schema
- Stable IDs: same `(type, normalized name)` → same id across runs
- Provenance on every triple: `source_path`, `extracted_at`, `content_hash`
- Idempotent re-ingest: unchanged files → zero LLM extract calls
- Conflict policy: same `(subject, predicate, object)` from two sources keeps
  both provenance rows (or documented last-write-wins — pick one, tests pin it)

**Spec:** `pytest -m phase13`  
**Why it matters:** garbage-in graphs make confident wrong agents. Schema +
provenance is how you debug "why does the bot think Mira owns payments-api?"

---

## Phase 14 — Graph traversal agent · `docsbot/agent/`

**Goal:** answer questions by **walking the graph with tools**, not stuffing
the whole KG into the prompt.

**You build:**
- `tools.py` — `lookup_entity`, `list_entities`, `get_neighbors`,
  `follow_edge`, `get_entity` (attrs + provenance)
- `runner.py` — tool loop: plan → tool → observe → … → answer or refuse
- Path citations: every answer includes the **edge path** used as evidence
- Cycle detection (`visited` set), `max_hops`, disambiguation when names collide
- Refuse when the graph cannot support the claim (no hallucinated edges)

**Real-world constraints:**
- Tool results are the only factual substrate for the final answer
- Multi-hop golden questions require the **correct path**, not just a plausible
  sentence (see `evals/golden_kg.json`)
- Ambiguous names (`lookup` returns >1) must force a clarifying tool strategy,
  not pick silently

**Spec:** `pytest -m phase14`  
**Why it matters:** this is agentic RAG done honestly — the graph is the
database; the LLM is the query planner.

---

## Phase 15 — Stateful sessions + checkpoints · `docsbot/agent/state.py`

**Goal:** conversations and mid-traversal agent state **survive process death**.

**You build:**
- SQLite session store: messages, working memory, checkpoint blob
- Checkpoint after every tool call (so a crash mid-hop can resume)
- Resume API: `continue_session(session_id)` restores tool loop state
- Working-memory compaction: keep entity IDs + path so far; drop raw tool JSON
  after N turns (cost + context control)
- Session TTL / delete

**Real-world constraints:**
- Resume must not re-run completed tool calls (exactly-once from the agent's
  point of view)
- Concurrent appends to the same session are serialized (SQLite busy timeout
  or explicit lock)
- "What did we already establish about payments-api?" uses session state, not
  a fresh cold graph walk every time

**Spec:** `pytest -m phase15`  
**Why it matters:** demos are stateless; production agents are interrupted,
retried, and continued by a different worker.

---

## Phase 16 — Cost efficiency · `docsbot/agent/budget.py`, caches

**Goal:** the agent is useful **and** stays inside an explicit budget.

**You build:**
- `Budget`: `max_hops`, `max_tool_calls`, `max_llm_calls`, `max_input_tokens`,
  `max_usd` — any breach → `BudgetExceeded` and a partial trace-friendly error
- Extraction cache: `content_hash → GraphDelta` (never re-extract unchanged docs)
- Graph read cache: hot `get_neighbors` for the turn
- Early stop: if a tool path already answers the question type, don't keep
  hopping "for completeness"
- Model routing knobs in `config.py`: cheap model for extract, stronger for
  final answer (even if both are free-tier names — practice the pattern)

**Real-world constraints:**
- Budget is checked **before** each LLM/tool call, not after the bill
- Cost ledger (Phase 9 ideas) records estimated USD per session
- A golden "budget trap" question fails the agent if it wanders > N hops

**Spec:** `pytest -m phase16`  
**Why it matters:** unbounded tool loops are how free-tier demos die and how
prod bills explode.

---

## Phase 17 — Phoenix observability + ops drills · `docsbot/tracing/`

**Goal:** when (not if) the agent is wrong or expensive, you **find it in
traces** and fix the cause.

> Package is named `tracing` on purpose — Part 2 already owns
> `docsbot/observability.py` (UsageLedger / structured logs).

**You build:**
- `phoenix_setup.py` — `register()` with project name, endpoint from env
- Manual/OpenInference spans: `ingest`, `extract`, `agent_turn`, `tool`, `llm`
- Attributes: session_id, entity ids, hop index, budget remaining, token usage
- Redaction: never put API keys or raw passwords into span attributes
- `evals/run_kg_evals.py` — score path correctness + refusal quality; optional
  export of spans for offline review

**Ops drills (manual, documented in corpus README):**
1. **Wrong owner** — corrupt one triple; find the bad provenance in Phoenix;
   fix source doc; re-ingest; confirm new trace path.
2. **Cost spike** — disable cache; watch tool/LLM span counts; re-enable;
   confirm budget + cache cut calls.
3. **Stuck loop** — introduce a cycle; confirm cycle detection + budget stop
   appear as clear span events, not a hang.

**Spec:** `pytest -m phase17` (contract tests with a fake tracer/exporter)  
**Live:** `docker compose --profile obs up` then run the drills.

**Why it matters:** without traces, graph agents are undebuggable. Phoenix is
the cockpit.

---

## Suggested order & definition of done

1. **13** — ingest Northstar; query the SQLite graph by hand; provenance sane.
2. **14** — agent answers golden multi-hop questions with correct paths.
3. **15** — kill -9 mid-run; resume; no duplicate tool side effects.
4. **16** — budget trap questions fail closed; caches cut extract calls to ~0
   on re-ingest.
5. **17** — traces visible in Phoenix; drills documented with before/after.

A phase is **done** when its `pytest -m phaseN` is fully green.

## Stretch (after 17)

- Hybrid: vector recall to propose seed entities, then graph walk
- Human-in-the-loop confirmation for irreversible / high-cost hops
- Graph migrations when the ontology gains a new relation type
- Wire the KG agent behind the Phase 11 `/ask` route with a `mode=graph` flag
- CI job: `pytest -m phase3` + spin Phoenix in compose for a smoke trace
