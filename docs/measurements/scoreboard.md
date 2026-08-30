| Metric | Before (broken) | After (fixed) | Change | Budget |
|---|---|---|---|---|
| Indexing: provider calls (49 chunks) | 49 | 1 | 49× fewer | — |
| Indexing: wall clock | 0.746 s | 0.025 s | 30× faster | — |
| Ask p95 latency (serial) | 0.273 s | 0.090 s | 3× faster | 0.130 s ✓ |
| Ask p95 latency (8 concurrent) | 1.416 s | 0.133 s | 10.6× faster | 0.130 s (borderline) |
| Total wall, 5 asks @ concurrency 8 | 1.420 s | 0.135 s | 10.5× | — |
| Provider calls per ask | 11 (8 = rerank) | 4 | 2.75× fewer | 4 ✓ |
| Input tokens per ask | 7,553 | 2,529 | 3× fewer | 3,200 ✓ |
| Cost per ask | $0.000653 | $0.000267 | 2.4× cheaper | $0.000340 ✓ |
| Turn-10 conversation tokens | 10,991 (rising from 8,313) | 3,277 (flat) | 3.4× fewer, no growth | — |
| Self-time breakdown available | no (metrics not yet instrumented) | yes | — | — |
| Perf/regression specs | 24 fail / 62 unrun | 94/94 pass | — | — |
