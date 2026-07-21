# DocsBot — Phase Two: from toy to product

Phase one (Phases 0–7) got you a working RAG bot on the free tier. It runs on
the happy path, in memory, on your laptop. **Phase two turns it into a real
application** — one that persists, survives a flaky network, retrieves better,
serves over HTTP behind auth, and has a UI a human can actually use.

```
8 Persist → 9 Reliability → 10 Retrieval+Eval → 11 API → 12 Frontend
  real DB     retries,         hybrid search,      FastAPI    React app:
  + incr.     backoff,         BM25+RRF+rerank,    + JWT      streaming chat,
  index       logging, cost    LLM-as-judge        auth+SSE   login, citations
```

## How phase two is different (read this first)

Phase one shipped a **complete reference implementation** you could read. Phase
two does **not**. Every new module is a **stub** that raises
`NotImplementedError`, with a thorough docstring explaining *what* to build and
*why* — and a **failing test suite that is the spec**. You make the tests pass.
This is how real features get built: the contract (tests, types, interfaces)
comes first, the implementation is yours.

> The tests are not a suggestion. They are the definition of "done." When a
> phase's tests are green, that phase is correct — not "looks right to me."

### Running the specs

Phase-two tests are **deselected by default**, so a plain `pytest` still passes
on the phase-one reference. Opt into a phase explicitly:

```bash
pytest                  # default: phase-one reference only (stays green)
pytest -m phase8        # just Phase 8's failing spec — your to-do list
pytest -m phase2        # every phase-two spec at once
```

Most specs need **no API key** — they inject fakes for the network so you're
testing *your* logic, not Gemini. Install only the extras a phase needs:

```bash
pip install -e ".[dev]"              # phases 9 & 10 (pure logic)
pip install -e ".[dev,persist]"      # phase 8  (Chroma)
pip install -e ".[dev,api]"          # phase 11 (FastAPI)
```

---

## Phase 8 — Persistence + a real vector DB · `docsbot/store_persistent.py`
**Goal:** replace the in-memory store with a disk-backed **Chroma** collection,
and index **incrementally** so unchanged files aren't re-embedded.
**Concepts:** persistence, a real vector database, content hashing for stable
ids, upsert, distance-vs-similarity conventions, programming to an interface
(your store must be a drop-in for Phase 5's).
**Spec:** `pytest -m phase8` — ranking, persistence across "restart", and
"don't re-embed unchanged chunks."
**Why it matters:** re-embedding a real corpus every run is slow and, off the
free tier, expensive. Incremental indexing is what makes ingestion scale.

## Phase 9 — Reliability + observability · `docsbot/reliability.py`, `docsbot/observability.py`
**Goal:** survive 429s/5xx with exponential backoff + retries; emit structured
JSON logs; track tokens and cost in a ledger.
**Concepts:** transient vs permanent errors, capped exponential backoff,
dependency injection of `sleep` for instant tests, structured logging, cost
accounting (tokens are the unit of both spend and context limits).
**Spec:** `pytest -m phase9` — backoff schedule, retry/give-up logic, log
shape, ledger arithmetic.
**Why it matters:** the free tier *will* rate-limit you mid-eval. A demo crashes;
a system retries and keeps a receipt.

## Phase 10 — Advanced retrieval + answer-quality eval · `docsbot/retrieval.py`, `evals/judge.py`
**Goal:** hybrid search — hand-rolled **BM25** keyword ranking fused with vector
search via **Reciprocal Rank Fusion**, then an **LLM reranker**. Plus
**answer-quality** evals using **LLM-as-judge** (faithfulness + correctness),
going beyond Phase 7's retrieval hit-rate.
**Concepts:** BM25 (IDF, term saturation), rank fusion across incompatible score
scales, reranking, LLM-as-judge with structured (Pydantic) verdicts, aggregating
a rubric over a golden set.
**Spec:** `pytest -m phase10` — BM25 ranking, RRF, rerank ordering, judge
grading + aggregation. All with injected fakes; no key needed.
**Why it matters:** semantic search misses exact terms (error codes, names);
keyword search misses paraphrases. Real systems do both. And retrieving the
right chunk ≠ writing a correct answer — you have to measure the answer too.

## Phase 11 — The API · `docsbot/api/`
**Goal:** a FastAPI service: `GET /health`, `POST /auth/login` (issues a signed
**JWT**), and protected `POST /ask`, `POST /ask/stream` (**SSE** streaming), and
`POST /chat` (sessions with memory).
**Concepts:** HTTP API design, Pydantic request/response contracts, bearer-token
auth (sign-and-verify, no session store), dependency injection for testable
routes, streaming over HTTP with Server-Sent Events.
**Spec:** `pytest -m phase11` — health, auth accept/reject, protected routes,
the SSE token→citations→done protocol, chat sessions. The RAG engine is faked
via a dependency override.
**Run it:** `uvicorn docsbot.api.app:create_app --factory --reload`
**Why it matters:** a library only you can call isn't a product. This is the
seam the frontend talks to — and the auth is the difference between a service
and an open wallet.

## Phase 12 — The frontend · `frontend/`
**Goal:** a real **React + TypeScript** app: a login screen, a chat UI that
**streams** the answer token-by-token (consuming `/ask/stream`), and a citations
panel. See `frontend/PHASE_12.md` for the full spec.
**Concepts:** calling an HTTP API from the browser, storing/sending a bearer
token, reading an SSE stream by hand with `fetch` + `getReader()` (because
`EventSource` can't send an auth header), React state for incremental rendering.
**Spec:** `cd frontend && npm install && npm test` — Vitest specs fail against
the stubs and pass when you build the four source files.
**Run it:** `npm run dev` (point `VITE_API_BASE` at your Phase 11 server).
**Why it matters:** this is the whole thing, finally usable by a human — the
payoff that makes every earlier phase concrete.

---

## Suggested order & checkpoints

1. **Phase 8** first — everything downstream benefits from a store that persists.
2. **Phase 9** next — you'll want retries the moment you run evals in a loop.
3. **Phase 10** — measurably improve retrieval, and learn to grade answers.
4. **Phase 11** — wrap it in a service. Test with `curl` before the UI exists.
5. **Phase 12** — the frontend, talking to your Phase 11 server.

A phase is **done** when its `pytest -m phaseN` (or `npm test`) is fully green —
no exceptions, no "close enough."

## Stretch goals (once Phase 12 runs end to end)

- Swap the in-memory `/chat` session store for Redis; discuss what breaks at
  scale without it.
- Stream `/chat` too (not just `/ask`), and stream true model tokens rather than
  re-chunking a finished answer.
- Add rate limiting and per-user quotas to the API.
- Wire the cost ledger (Phase 9) into the API and show spend per request in the
  UI.
- Containerize the whole stack (API + frontend) with Docker Compose.
- Add CI: run `pytest -m phase2` and `npm test` on every push.
```
