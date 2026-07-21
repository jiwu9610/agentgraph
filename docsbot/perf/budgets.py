"""Performance budgets and the regression gate.

Once a performance fix lands, what stops the next person — or the same person,
in five weeks, at 6pm on a Friday — from putting the regression back?

Nothing, unless the budget is executable. A number in a wiki is a wish. A
number in CI is a constraint. This module turns the four numbers that matter
into a test that fails the build when someone regresses them:

    p95 latency per ask       <= settings.budget_ask_p95_s
    input tokens per ask      <= settings.budget_ask_input_tokens
    provider calls per ask    <= settings.budget_ask_provider_calls
    USD per ask               <= settings.budget_ask_usd

Set them at *today's* measured numbers plus a little headroom. A budget with
10x slack isn't a budget, it's a formality — it will never fail, including when
it should. A budget with no slack fails on CI machine noise and gets disabled
within a week, which is worse than not having one. Aim for roughly 20-30%.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class Budget:
    """One enforceable limit.

    `name` matches a key in the observed-metrics dict, e.g. "ask.p95_s".
    """

    name: str
    limit: float
    unit: str = ""


@dataclass(frozen=True)
class Violation:
    """One budget that was exceeded, with enough detail to act on."""

    budget: Budget
    observed: float

    @property
    def overage_ratio(self) -> float:
        """observed / limit — 1.0 is exactly at budget, 2.0 is double."""
        if self.budget.limit == 0:
            return float("inf") if self.observed > 0 else 1.0
        return self.observed / self.budget.limit

    def __str__(self) -> str:
        unit = f" {self.budget.unit}" if self.budget.unit else ""
        return (f"{self.budget.name}: {self.observed:g}{unit} exceeds budget "
                f"{self.budget.limit:g}{unit} ({self.overage_ratio:.2f}x)")


def default_budgets() -> list[Budget]:
    """The four budgets above, read from `settings`.

    Names must be exactly: "ask.p95_s", "ask.input_tokens", "ask.provider_calls",
    "ask.usd" — the gate test looks them up by name.
    """
    return [
        Budget("ask.p95_s", settings.budget_ask_p95_s, "s"),
        Budget("ask.input_tokens", settings.budget_ask_input_tokens, "tokens"),
        Budget("ask.provider_calls", settings.budget_ask_provider_calls, "calls"),
        Budget("ask.usd", settings.budget_ask_usd, "USD"),
    ]


def check(observed: dict[str, float],
          budgets: list[Budget] | None = None) -> list[Violation]:
    """Compare measurements against budgets.

    - Defaults to `default_budgets()` when `budgets` is None.
    - Returns one `Violation` per exceeded budget, worst overage first.
    - A budget whose metric is missing from `observed` is SKIPPED, not failed:
      a gate that fails on absent data trains people to ignore it.
    - Exactly at the limit passes. Over it fails.
    """
    budgets = default_budgets() if budgets is None else budgets
    violations = [
        Violation(budget=b, observed=observed[b.name])
        for b in budgets
        if b.name in observed and observed[b.name] > b.limit
    ]
    violations.sort(key=lambda v: v.overage_ratio, reverse=True)
    return violations
