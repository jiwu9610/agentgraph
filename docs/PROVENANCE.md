# Provenance

Component-level record of what was provided to this project and what was implemented
for it. The README carries the summary; this file is the detail.

## Provided — a mentor's reference stack (used with permission; not licensed for reuse)

| Material | Location |
|---|---|
| Test specifications (the phase-marked suites) | `tests/` — all files except `tests/test_api_extensions.py` |
| Sample corpora | `corpus/handbook/`, `corpus/northstar/`, and the teaching notes in `docs/*.md` |
| Incident scenarios (fictional personas, intentionally planted failure modes) | `incidents/` |
| Reference modules | `docsbot/chat.py`, `cli.py`, `client.py`, `extract.py`, `ingest.py`, `store.py`, `tools.py` |
| Instrumented performance service and measurement harness — including the performance-tuning fixes measured in the study | `docsbot/service/`, `docsbot/perf/` |
| Layer design notes | `docs/design/` |

## Implemented for this project (Jiezhong Wu)

| Component | Location |
|---|---|
| Knowledge-graph stack: closed-world schema, provenance store, guarded extraction, idempotent content-hash ingest | `docsbot/kg/` |
| Budgeted traversal agent: grounding gate, futility guard, checkpointed resumable sessions | `docsbot/agent/` |
| Tracing: OTel/Phoenix span helpers with a single redaction choke point | `docsbot/tracing/` |
| Serving stack, implemented against the provided specifications: persistence, hybrid retrieval, reliability, observability, LLM-judge grading | `docsbot/store_persistent.py`, `retrieval.py`, `reliability.py`, `observability.py`, `evals/judge.py` |
| API service: auth, SSE streaming, fail-open rate limiting, Redis sessions, cost events, citation snippets | `docsbot/api/` (plus extensions to `docsbot/rag.py` and `docsbot/config.py`, which build on reference cores) |
| React/TypeScript frontend | `frontend/` |
| Extension test suites for previously unspecified behaviors | `tests/test_api_extensions.py`, `frontend/src/extensions.test.tsx` |
| Deployment and CI | `Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` |
| Engineering decision log and eval-hardening record | `DECISIONS.md` |
| Performance study: controlled measurements and root-cause postmortems | `PART4_POSTMORTEMS.md`, `docs/measurements/` |

## Notes

- Development was AI-assisted throughout; the git commit trailers record that
  collaboration.
- For the performance study, the measured fixes ship with the reference baseline; the
  instrumentation, controlled before/after measurements, and root-cause analyses are
  project work — which is why the study's verbs are *measured*, *verified*, and
  *analyzed* rather than *fixed*.
- The serving-stack implementation was produced against the provided specifications and
  behavior contracts without consulting any prior implementation of those layers.
