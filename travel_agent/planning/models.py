"""Planning-specific results serialized at the existing dictionary boundary."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HardConstraintFailure:
    code: str
    minutes_late: float


@dataclass(frozen=True)
class FeasibilityResult:
    feasible: bool
    gate_arrival_time: str
    gate_deadline: str
    gate_close_buffer_minutes: int
    deadline_source: str
    hard_constraint_failures: tuple[HardConstraintFailure, ...]

    def to_dict(self):
        result = asdict(self)
        result["hard_constraint_failures"] = [asdict(f) for f in self.hard_constraint_failures]
        return result


@dataclass(frozen=True)
class ScoreEvaluation:
    score: float
    score_breakdown: dict
    calendar_conflicts: list[dict]

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SearchOutcome:
    finalists: list[dict]
    diagnostics: dict
