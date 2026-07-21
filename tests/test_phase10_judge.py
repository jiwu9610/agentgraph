"""Answer-quality eval spec (LLM-as-judge).

The judge model is injected as a fake so the grading + aggregation logic is
tested deterministically, no key required.

    pytest -m phase10
"""

import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase10]

import sys
from pathlib import Path

# evals/ isn't a package; put it on the path so `import judge` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from judge import Verdict, aggregate, grade_answer  # noqa: E402


def test_grade_answer_passes_inputs_to_judge_and_returns_verdict():
    seen = {}

    def fake_judge(prompt):
        seen["prompt"] = prompt
        return Verdict(faithfulness=0.9, correctness=1.0, reason="grounded and correct")

    v = grade_answer(
        question="Which task type for documents?",
        answer="Use RETRIEVAL_DOCUMENT (embeddings.md).",
        context="Documents are embedded with RETRIEVAL_DOCUMENT.",
        expected="RETRIEVAL_DOCUMENT",
        judge=fake_judge,
    )
    assert isinstance(v, Verdict)
    assert v.correctness == 1.0
    # The rubric prompt must actually include the answer and the reference.
    assert "RETRIEVAL_DOCUMENT" in seen["prompt"]


def test_aggregate_computes_means_and_pass_rate():
    verdicts = [
        Verdict(faithfulness=1.0, correctness=1.0, reason="ok"),    # pass
        Verdict(faithfulness=0.8, correctness=0.6, reason="ok"),    # pass
        Verdict(faithfulness=0.9, correctness=0.2, reason="wrong"), # fail (correctness < .5)
        Verdict(faithfulness=0.1, correctness=0.9, reason="made up"), # fail (faithfulness < .5)
    ]
    agg = aggregate(verdicts)

    assert agg["n"] == 4
    assert agg["mean_faithfulness"] == pytest.approx((1.0 + 0.8 + 0.9 + 0.1) / 4)
    assert agg["mean_correctness"] == pytest.approx((1.0 + 0.6 + 0.2 + 0.9) / 4)
    assert agg["pass_rate"] == pytest.approx(0.5)  # 2 of 4 pass BOTH dims
