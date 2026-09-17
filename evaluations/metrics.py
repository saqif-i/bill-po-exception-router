"""Scoring for the evaluation harness.

INSUFFICIENT_EVIDENCE is scored as CORRECT when the label says the text does not
support a view. A harness optimised only for agreement would train the system
out of the most useful answer it can give.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tally:
    total: int = 0
    invoked: int = 0
    correct: int = 0
    by_recommendation: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    gate_excluded: int = 0
    gate_leaks: list[str] = field(default_factory=list)

    def record_gate_exclusion(self, case_id: str, excluded: bool) -> None:
        if excluded:
            self.gate_excluded += 1
        else:
            self.gate_leaks.append(case_id)

    def record(self, *, expected: str, actual: str | None, rejection: str | None) -> None:
        self.total += 1
        if rejection:
            self.rejected[rejection] = self.rejected.get(rejection, 0) + 1
            return
        self.invoked += 1
        self.by_recommendation[actual] = self.by_recommendation.get(actual, 0) + 1
        if actual == expected:
            self.correct += 1

    def report(self) -> dict:
        return {
            "cases": self.total,
            "invoked": self.invoked,
            "agreement_rate": round(self.correct / self.invoked, 3) if self.invoked else None,
            "by_recommendation": self.by_recommendation,
            "rejections": self.rejected,
            "gate_excluded": self.gate_excluded,
            "gate_leaks": self.gate_leaks,
            "gate_exclusion_is_complete": not self.gate_leaks,
        }
