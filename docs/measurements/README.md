# Measurement artifacts

Raw output of the `python -m docsbot.perf.report` runs behind the before/after results in
the top-level README and [PART4_POSTMORTEMS.md](../../PART4_POSTMORTEMS.md).

| File | What it holds |
|---|---|
| `baseline-before.txt` | The planted-pathology baseline: indexing cost and serial asks (n=5 and n=20) |
| `baseline-before-extra.txt` | Same baseline: 8-way concurrency, and per-turn conversation cost growth |
| `after-fixes.txt` | The fixed pipeline: full spec run (94/94), stage self-time table, concurrency, flat conversation cost |
| `scoreboard.md` | The summary table |

**How they were produced.** Both configurations ran on equivalent dedicated compute nodes
(labeled `compute-node-A`/`compute-node-B`) on 2026-08-21, same interpreter and settings.
Run headers record date and compute node; `baseline-before-extra.txt` additionally pins
the exact checkout under test (its "code under test" line) — the package is installed
editable, so this guards against silently measuring the wrong tree.

**What they measure.** All numbers come from the deterministic instrumented harness
(`docsbot/perf/harness.py`): a fake provider with realistic latency and pricing models that
counts every call, token, and dollar. They characterize the pipeline's behavior under a
controlled provider — not live-provider performance — and the underlying failure modes were
intentionally planted as production-debugging exercises.

**Reproduce.**

```bash
pip install -e ".[dev]"                        # offline; no API key needed
python -m docsbot.perf.report --asks 20
python -m docsbot.perf.report --concurrency 8
python -m docsbot.perf.report --turns 10
python -m docsbot.perf.report --index
```
