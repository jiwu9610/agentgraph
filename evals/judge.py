"""Answer-quality grading via LLM-as-judge.

Retrieval metrics say whether the right chunk was fetched; they say nothing
about whether the final answer is correct or grounded. Free-text answers can't
be compared with equality, so a second model call grades each answer against a
reference on two rubric dimensions:

  - faithfulness: every claim in the answer is supported by the retrieved
    context (catches hallucination).
  - correctness: the answer contains the expected fact from the reference.

The judge is forced to return structured output (a ``Verdict``) rather than
prose, which keeps grading machine-readable and lets tests inject a
deterministic fake judge. Aggregation over a set of verdicts is pure
arithmetic — no model calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Verdict(BaseModel):
    """A judge's structured grade of one answer. This is the rubric contract."""

    faithfulness: float = Field(description="0-1: is the answer grounded in the context?")
    correctness: float = Field(description="0-1: does it contain the expected fact?")
    reason: str = Field(description="One sentence justifying the scores.")


JUDGE_SYSTEM = (
    "You are a strict grader. Given a question, a model's answer, the retrieved "
    "context it was given, and a reference answer, score the answer's "
    "faithfulness (grounded in context?) and correctness (matches reference?) "
    "from 0 to 1. Be harsh about unsupported claims."
)


def _default_judge(prompt: str) -> Verdict:
    """Production judge: call the chat model with a Verdict response schema.

    Imported lazily so the eval helpers stay usable (and testable with a fake
    judge) without the model client stack.
    """
    from google.genai import types

    from docsbot.client import get_client
    from docsbot.config import settings

    response = get_client().models.generate_content(
        model=settings.chat_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=JUDGE_SYSTEM,
            response_mime_type="application/json",
            response_schema=Verdict,
        ),
    )
    # .parsed is a Verdict instance because a Pydantic schema was passed.
    return response.parsed  # type: ignore[return-value]


def grade_answer(
    question: str,
    answer: str,
    context: str,
    expected: str,
    *,
    judge=None,
) -> Verdict:
    """Grade one answer, returning a typed Verdict.

    Builds a rubric prompt from the four inputs and hands it to ``judge``,
    a callable ``(prompt: str) -> Verdict``. Tests inject a deterministic
    fake; by default the chat model is called with structured output so a
    parsed Verdict comes back, not prose.
    """
    if judge is None:
        judge = _default_judge

    prompt = (
        f"{JUDGE_SYSTEM}\n\n"
        f"QUESTION:\n{question}\n\n"
        f"ANSWER TO GRADE:\n{answer}\n\n"
        f"RETRIEVED CONTEXT:\n{context}\n\n"
        f"REFERENCE ANSWER:\n{expected}\n"
    )
    return judge(prompt)


def aggregate(verdicts: list[Verdict]) -> dict:
    """Summarize a set of verdicts.

    Returns "mean_faithfulness" and "mean_correctness" (averages), "pass_rate"
    (fraction of verdicts where BOTH dimensions are >= 0.5), and "n" (count).
    An empty list yields zeros rather than a division error.
    """
    n = len(verdicts)
    if n == 0:
        return {"mean_faithfulness": 0.0, "mean_correctness": 0.0, "pass_rate": 0.0, "n": 0}

    passed = sum(1 for v in verdicts if v.faithfulness >= 0.5 and v.correctness >= 0.5)
    return {
        "mean_faithfulness": sum(v.faithfulness for v in verdicts) / n,
        "mean_correctness": sum(v.correctness for v in verdicts) / n,
        "pass_rate": passed / n,
        "n": n,
    }
