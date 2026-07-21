"""Observability specs — structured logging + the usage/cost ledger.

All pure: no network, no key. We capture log output to a StringIO and check the
ledger's arithmetic against the config prices.

    pytest -m phase9
"""

import io
import json

import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase9]

from docsbot.config import settings
from docsbot.observability import UsageLedger, log_event


# --- structured logging -----------------------------------------------------

def test_log_event_emits_one_json_line():
    buf = io.StringIO()
    log_event("query", stream=buf, question="what is RAG?", n_hits=4)
    out = buf.getvalue()

    assert out.endswith("\n")
    assert out.count("\n") == 1                 # exactly one line per event
    record = json.loads(out)                    # valid JSON, not prose
    assert record["event"] == "query"
    assert record["question"] == "what is RAG?"
    assert record["n_hits"] == 4


# --- usage ledger -----------------------------------------------------------

def test_ledger_accumulates_tokens_and_calls():
    led = UsageLedger()
    led.record(input_tokens=100, output_tokens=20)
    led.record(input_tokens=50, output_tokens=10)

    assert led.calls == 2
    assert led.input_tokens == 150
    assert led.output_tokens == 30


def test_ledger_cost_uses_config_prices():
    led = UsageLedger()
    led.record(input_tokens=1_000_000, output_tokens=1_000_000)
    expected = settings.usd_per_1m_input + settings.usd_per_1m_output
    assert led.cost_usd == pytest.approx(expected)


def test_ledger_summary_is_a_dict_snapshot():
    led = UsageLedger()
    led.record(input_tokens=200, output_tokens=40)
    summary = led.summary()

    assert isinstance(summary, dict)
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 200
    assert summary["output_tokens"] == 40
    assert summary["cost_usd"] == pytest.approx(led.cost_usd)
