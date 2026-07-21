"""Performance toolkit: measurement harness, metrics, budgets.

Three things live here:

  harness.py   A deterministic fake model provider. It counts every call, every
               token, every dollar, and it sleeps a realistic amount. This is
               the GROUND TRUTH the perf specs assert against — do not edit it
               to make a test pass; editing it invalidates every measurement.

  metrics.py   In-process instrumentation: spans, counters, percentiles.

  budgets.py   Declarative performance budgets + the regression gate.

`report.py` is the measurement CLI that ties them together:

    python -m docsbot.perf.report

Run it before changing anything. Run it after. The delta is the evidence.
"""

from .harness import CallRecord, FakeProvider, ProviderError

__all__ = ["CallRecord", "FakeProvider", "ProviderError"]
