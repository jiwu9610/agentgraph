"""Structured event logging and a token-usage/cost ledger.

Two small primitives:

- `log_event` writes one JSON object per line, so logs are greppable,
  countable data rather than prose.
- `UsageLedger` accumulates per-call token usage and converts it to dollars
  with the per-million-token prices in config. Tokens are the unit of both
  cost and context budgets, so the running totals are worth watching from the
  first call.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TextIO

from .config import settings


def log_event(event: str, *, stream: TextIO = sys.stdout, **fields: object) -> None:
    """Write one structured JSON log line: {"event": event, ...fields}.

    Exactly one JSON object per line (newline-terminated) so each event is an
    independently parseable record. `stream` is injectable so tests can capture
    output instead of writing to the real stdout.
    """
    stream.write(json.dumps({"event": event, **fields}) + "\n")


@dataclass
class UsageLedger:
    """Running totals of token usage and the dollar cost they imply."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Add one model call's usage to the running totals."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    @property
    def cost_usd(self) -> float:
        """Total cost so far, from the per-million-token prices in config."""
        return (
            self.input_tokens / 1e6 * settings.usd_per_1m_input
            + self.output_tokens / 1e6 * settings.usd_per_1m_output
        )

    def summary(self) -> dict:
        """A snapshot dict of calls, tokens, and cost — ready to log or print."""
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }
