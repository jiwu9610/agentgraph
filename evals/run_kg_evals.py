"""Score graph-agent answers against evals/golden_kg.json.

Usage:
    python evals/run_kg_evals.py

Exit code 1 if any non-budget-trap case fails path/refusal checks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def path_matches(actual: list[tuple[str, str, str]], suffixes: list[list[list[str]]]) -> bool:
    if not suffixes:
        return True
    actual_triples = [(a, b, c) for a, b, c in actual]
    for suffix in suffixes:
        needed = [(s[0], s[1], s[2]) for s in suffix]
        # subsequence match — agent may include extra hops
        i = 0
        for hop in actual_triples:
            if i < len(needed) and hop == needed[i]:
                i += 1
        if i == len(needed):
            return True
    return False


def main() -> int:
    from docsbot.agent.budget import Budget
    from docsbot.agent.runner import run_agent
    from docsbot.kg.ingest import ingest_corpus
    from docsbot.kg.store import KnowledgeGraphStore

    golden = json.loads((ROOT / "evals" / "golden_kg.json").read_text())
    db = ROOT / ".docsbot_kg_eval.sqlite"
    if db.exists():
        db.unlink()
    store = KnowledgeGraphStore(db)
    ingest_corpus(ROOT / "corpus" / "northstar", store)

    failed = 0
    for case in golden["cases"]:
        if case.get("budget_trap"):
            budget = Budget(max_hops=case.get("max_hops", 4), max_tool_calls=6)
            try:
                ans = run_agent(case["question"], store, budget=budget)
                # Trap passes if budget stopped the wander OR path stayed tiny
                ok = ans.budget_snapshot.get("hops", 99) <= budget.max_hops
            except Exception as exc:  # BudgetExceeded is success for the trap
                ok = "Budget" in type(exc).__name__ or "budget" in str(exc).lower()
                ans = None
            status = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            print(f"{status} {case['id']} (budget trap)")
            continue

        ans = run_agent(case["question"], store, budget=Budget(max_hops=8, max_tool_calls=16))
        actual = [(h.subject_id, h.predicate, h.object_id) for h in ans.path]
        refuse_ok = ans.refused == case["expect_refused"]
        path_ok = case["expect_refused"] or path_matches(
            actual, case.get("acceptable_path_suffixes", [])
        )
        mention_ok = all(m.lower() in ans.text.lower() for m in case.get("answer_must_mention", []))
        ok = refuse_ok and path_ok and (case["expect_refused"] or mention_ok)
        if not ok:
            failed += 1
        print(
            f"{'PASS' if ok else 'FAIL'} {case['id']} "
            f"refused={ans.refused} path={actual!r}"
        )

    store.close()
    print(f"\n{len(golden['cases']) - failed}/{len(golden['cases'])} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
