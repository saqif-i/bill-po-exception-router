"""Scoring for the evaluation harness.

INSUFFICIENT_EVIDENCE is scored as CORRECT when the label says the text does not
support a view. A harness optimised only for agreement would train the system
out of the most useful answer it can give.

Every case sent to the model counts as a model call, whether the answer was
used or withheld (low confidence, refused, invalid). Agreement is measured over
the answers that were used. Each case is reported individually, so a claim such
as "the two withheld cases were the two labelled INSUFFICIENT_EVIDENCE" can be
checked against the output rather than inferred from the totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tally:
    total: int = 0
    invoked: int = 0
    answered: int = 0
    correct: int = 0
    by_recommendation: dict[str, int] = field(default_factory=dict)
    withheld: dict[str, int] = field(default_factory=dict)
    per_case: list[dict] = field(default_factory=list)
    gate_excluded: int = 0
    gate_leaks: list[str] = field(default_factory=list)

    def record_gate_exclusion(self, case_id: str, excluded: bool) -> None:
        if excluded:
            self.gate_excluded += 1
        else:
            self.gate_leaks.append(case_id)

    def record(
        self, *, case_id: str, expected: str, actual: str | None, withheld: str | None
    ) -> None:
        """One case that reached the model. `withheld` is the reason its answer
        was not used, or None when it was."""
        self.total += 1
        self.invoked += 1
        if withheld:
            self.withheld[withheld] = self.withheld.get(withheld, 0) + 1
            agrees = None
        else:
            self.answered += 1
            self.by_recommendation[actual] = self.by_recommendation.get(actual, 0) + 1
            agrees = actual == expected
            self.correct += agrees
        self.per_case.append(
            {
                "id": case_id,
                "expected": expected,
                "actual": actual,
                "withheld": withheld,
                "agrees": agrees,
            }
        )

    def report(self) -> dict:
        return {
            "cases": self.total,
            "invoked": self.invoked,
            "answered": self.answered,
            "agreement_rate": round(self.correct / self.answered, 3) if self.answered else None,
            "by_recommendation": self.by_recommendation,
            "withheld": self.withheld,
            "per_case": self.per_case,
            "gate_excluded": self.gate_excluded,
            "gate_leaks": self.gate_leaks,
            "gate_exclusion_is_complete": not self.gate_leaks,
        }
