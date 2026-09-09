"""Configured planning policy, not an airline-provided gate-close deadline."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanningPolicy:
    gate_close_buffer_minutes: int = 15

    def __post_init__(self):
        if type(self.gate_close_buffer_minutes) is not int or self.gate_close_buffer_minutes < 0:
            raise ValueError("gate_close_buffer_minutes must be a non-negative integer")


DEFAULT_PLANNING_POLICY = PlanningPolicy()
