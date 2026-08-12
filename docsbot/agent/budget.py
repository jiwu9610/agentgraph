"""Hard budgets for agent turns.

Check **before** each LLM or tool call. Production systems that check after
the fact only write nicer postmortems.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a gate would be breached by the next action."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass
class Budget:
    max_hops: int = 6
    max_tool_calls: int = 12
    max_llm_calls: int = 8
    max_input_tokens: int = 50_000
    max_usd: float = 0.05

    hops: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd_spent: float = 0.0
    cache_hits: int = 0

    # Rough prices — reuse config in a real impl; defaults keep tests offline.
    usd_per_1m_input: float = 0.075
    usd_per_1m_output: float = 0.30

    def remaining(self) -> dict[str, float | int]:
        return {
            "hops": self.max_hops - self.hops,
            "tool_calls": self.max_tool_calls - self.tool_calls,
            "llm_calls": self.max_llm_calls - self.llm_calls,
            "input_tokens": self.max_input_tokens - self.input_tokens,
            "usd": round(self.max_usd - self.usd_spent, 6),
            "cache_hits": self.cache_hits,
        }

    def check_tool(self) -> None:
        """Raise BudgetExceeded if another tool call is not allowed."""
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(
                "tool_calls",
                f"tool call budget exhausted ({self.tool_calls}/{self.max_tool_calls})",
            )

    def check_llm(self, estimated_input_tokens: int = 0) -> None:
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(
                "llm_calls",
                f"LLM call budget exhausted ({self.llm_calls}/{self.max_llm_calls})",
            )
        if self.input_tokens + estimated_input_tokens > self.max_input_tokens:
            raise BudgetExceeded(
                "input_tokens",
                f"input token budget would be exceeded "
                f"({self.input_tokens} + {estimated_input_tokens} > {self.max_input_tokens})",
            )
        if self.usd_spent >= self.max_usd:
            raise BudgetExceeded(
                "usd", f"spend cap reached (${self.usd_spent:.4f}/${self.max_usd:.4f})"
            )

    def check_hop(self) -> None:
        if self.hops >= self.max_hops:
            raise BudgetExceeded(
                "hops", f"hop budget exhausted ({self.hops}/{self.max_hops})"
            )

    def record_tool(self) -> None:
        self.tool_calls += 1

    def record_hop(self) -> None:
        self.hops += 1

    def record_llm(self, input_tokens: int, output_tokens: int) -> None:
        """Increment counters and usd_spent from token prices."""
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.usd_spent += (
            input_tokens / 1_000_000 * self.usd_per_1m_input
            + output_tokens / 1_000_000 * self.usd_per_1m_output
        )

    def record_cache_hit(self) -> None:
        self.cache_hits += 1


@dataclass
class TurnCache:
    """Per-turn memo for graph reads / extraction hashes."""

    neighbor_cache: dict[tuple[str, str | None, str], list] = field(default_factory=dict)
    extract_cache: dict[str, object] = field(default_factory=dict)

    def get_neighbors(self, key: tuple[str, str | None, str]) -> list | None:
        return self.neighbor_cache.get(key)

    def put_neighbors(self, key: tuple[str, str | None, str], value: list) -> None:
        self.neighbor_cache[key] = value
