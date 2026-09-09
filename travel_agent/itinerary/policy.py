from dataclasses import dataclass


@dataclass(frozen=True)
class ActivationPolicy:
    planning_lead_time_minutes: int = 1440

    def __post_init__(self):
        if type(self.planning_lead_time_minutes) is not int or self.planning_lead_time_minutes < 0:
            raise ValueError("planning_lead_time_minutes must be a non-negative integer")


DEFAULT_ACTIVATION_POLICY = ActivationPolicy()
