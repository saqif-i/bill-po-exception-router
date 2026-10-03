"""Evaluation scoring. A withheld answer was still a model call."""

from __future__ import annotations

from evaluations.metrics import Tally


def _tally() -> Tally:
    tally = Tally()
    tally.record(
        case_id="sem-01", expected="LIKELY_EQUIVALENT", actual="LIKELY_EQUIVALENT", withheld=None
    )
    tally.record(
        case_id="sem-03", expected="LIKELY_DIFFERENT", actual="LIKELY_EQUIVALENT", withheld=None
    )
    tally.record(
        case_id="sem-05",
        expected="INSUFFICIENT_EVIDENCE",
        actual=None,
        withheld="SEMANTIC_LOW_CONFIDENCE",
    )
    return tally


def test_a_withheld_answer_counts_as_a_model_call() -> None:
    report = _tally().report()
    assert report["invoked"] == 3
    assert report["answered"] == 2
    assert report["withheld"] == {"SEMANTIC_LOW_CONFIDENCE": 1}


def test_agreement_is_measured_over_answers_that_were_used() -> None:
    assert _tally().report()["agreement_rate"] == 0.5


def test_every_case_is_reported_individually() -> None:
    per_case = {row["id"]: row for row in _tally().report()["per_case"]}
    assert per_case["sem-05"]["withheld"] == "SEMANTIC_LOW_CONFIDENCE"
    assert per_case["sem-05"]["agrees"] is None
    assert per_case["sem-01"]["agrees"] is True
    assert per_case["sem-03"]["agrees"] is False
