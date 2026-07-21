"""The measurement CLI — the tool the debugging is done with.

    python -m docsbot.perf.report                # baseline: one ask, warm
    python -m docsbot.perf.report --asks 20      # percentiles over 20 asks
    python -m docsbot.perf.report --concurrency 8
    python -m docsbot.perf.report --turns 10     # conversation cost growth
    python -m docsbot.perf.report --index        # what a re-index costs

Run it BEFORE changing anything and save the output. That's the baseline.
Every claim made later ("this cut cost 6x") is a diff against it. An
optimization without a before-and-after didn't happen.

The stage table comes from `Metrics` (metrics.py). If that is unavailable the
report still works — it just shows provider-side numbers only.
"""

from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..service.pipeline import DocsService, load_corpus
from .harness import FakeProvider
from .metrics import Metrics, percentile

QUESTIONS = [
    "Which service is the double-entry accounting core?",
    "What happens when risk-engine times out?",
    "What does edge-gateway do?",
    "Which database uses synchronous replication?",
    "What is stored in Redis and why?",
]


def _safe(fn, default=None):
    """Metrics may be unimplemented; degrade instead of crashing."""
    try:
        return fn()
    except NotImplementedError:
        return default


def _working_metrics() -> Metrics | None:
    """Return a Metrics only if the Metrics class is actually implemented.

    Otherwise hand the service None so it uses its no-op metrics and the rest
    of this report still runs — provider cost and latency are visible even
    without the span table.
    """
    m = Metrics()
    try:
        with m.span("probe"):
            m.count("probe")
        m.reset()
    except NotImplementedError:
        return None
    return m


def _bar(value: float, peak: float, width: int = 28) -> str:
    if peak <= 0:
        return ""
    return "█" * max(1, int(round(value / peak * width)))


def main() -> None:
    ap = argparse.ArgumentParser(description="DocsBot performance report")
    ap.add_argument("--asks", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--turns", type=int, default=0, help="run an N-turn conversation")
    ap.add_argument("--index", action="store_true", help="measure a re-index")
    args = ap.parse_args()

    corpus = load_corpus()
    provider = FakeProvider()
    metrics = _working_metrics()
    service = DocsService(provider, metrics=metrics)

    # --- indexing -----------------------------------------------------
    print("=" * 72)
    print("INDEXING")
    print("=" * 72)
    t0 = time.perf_counter()
    n_chunks = service.index(corpus)
    idx_wall = time.perf_counter() - t0
    idx = provider.summary()
    print(f"  chunks indexed      {n_chunks}")
    print(f"  wall clock          {idx_wall:8.3f} s")
    print(f"  provider calls      {idx['calls']:8d}   "
          f"({idx['embed_calls']} embed, {idx['chat_calls']} chat)")
    print(f"  cost                ${idx['usd']:.6f}")

    if args.index:
        provider.reset()
        t0 = time.perf_counter()
        service.index(corpus)
        re_wall = time.perf_counter() - t0
        re = provider.summary()
        print("\n  RE-index of the *same, unchanged* corpus:")
        print(f"    wall clock        {re_wall:8.3f} s")
        print(f"    provider calls    {re['calls']:8d}")
        print(f"    cost              ${re['usd']:.6f}")

    # --- asks ---------------------------------------------------------
    provider.reset()
    if metrics:
        _safe(metrics.reset)
    latencies: list[float] = []

    def one_ask(i: int) -> float:
        q = QUESTIONS[i % len(QUESTIONS)]
        t = time.perf_counter()
        service.ask(q)
        return time.perf_counter() - t

    print("\n" + "=" * 72)
    print(f"ASK  (n={args.asks}, concurrency={args.concurrency})")
    print("=" * 72)
    t0 = time.perf_counter()
    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            latencies = list(pool.map(one_ask, range(args.asks)))
    else:
        latencies = [one_ask(i) for i in range(args.asks)]
    total_wall = time.perf_counter() - t0

    s = provider.summary()
    p50 = _safe(lambda: percentile(latencies, 50)) or statistics.median(latencies)
    p95 = _safe(lambda: percentile(latencies, 95)) or max(latencies)

    print(f"  total wall clock    {total_wall:8.3f} s")
    print(f"  p50 latency         {p50:8.3f} s")
    print(f"  p95 latency         {p95:8.3f} s     (budget {settings.budget_ask_p95_s:.3f})")
    print(f"  provider calls      {s['calls']:8d}     "
          f"({s['calls'] / args.asks:.1f} per ask, budget {settings.budget_ask_provider_calls})")
    print(f"  input tokens        {s['input_tokens']:8d}     "
          f"({s['input_tokens'] / args.asks:.0f} per ask, "
          f"budget {settings.budget_ask_input_tokens})")
    print(f"  cost                ${s['usd']:.6f}     "
          f"(${s['usd'] / args.asks:.6f} per ask, budget ${settings.budget_ask_usd:.6f})")
    print(f"  cache               {service.cache.stats()}")

    print("\n  calls by stage:")
    for tag, n in sorted(s["calls_by_tag"].items(), key=lambda kv: -kv[1]):
        print(f"    {tag:<14} {n:6d}  {_bar(n, max(s['calls_by_tag'].values()))}")

    # --- where the time went (needs Metrics) --------------------------
    table = _safe(metrics.stage_table) if metrics else None
    print("\n  where the wall clock went (self time = not in a child span):")
    if not table:
        print("    [ no metrics recorded — Metrics instrumentation not active ]")
    else:
        peak = max((v["self_s"] for v in table.values()), default=0.0)
        rows = sorted(table.items(), key=lambda kv: -kv[1]["self_s"])
        print(f"    {'stage':<14}{'count':>6}{'total_s':>10}{'self_s':>10}{'p95_s':>9}")
        for name, v in rows:
            print(f"    {name:<14}{v['count']:>6}{v['total_s']:>10.3f}"
                  f"{v['self_s']:>10.3f}{v['p95_s']:>9.3f}  {_bar(v['self_s'], peak)}")

    counters = _safe(metrics.counters) if metrics else None
    if counters:
        print("\n  counters:")
        for k, v in sorted(counters.items()):
            print(f"    {k:<20} {v}")

    # --- conversation growth ------------------------------------------
    if args.turns:
        print("\n" + "=" * 72)
        print(f"CONVERSATION  ({args.turns} turns, one session)")
        print("=" * 72)
        provider.reset()
        print(f"    {'turn':>5}{'input_tokens':>15}{'cost_usd':>12}")
        for turn in range(1, args.turns + 1):
            before_tokens, before_usd = provider.input_tokens, provider.usd
            service.chat("sess-1", QUESTIONS[turn % len(QUESTIONS)])
            print(f"    {turn:>5}{provider.input_tokens - before_tokens:>15}"
                  f"{provider.usd - before_usd:>12.6f}")
        print(f"\n  sessions held in memory: {len(service.sessions)}")
        print("  (if turn-10 costs several times turn-1, you are resending the "
              "whole transcript every turn)")

    print()


if __name__ == "__main__":
    main()
