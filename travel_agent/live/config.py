"""Configuration references only; this phase neither loads secrets nor connects providers."""
from dataclasses import dataclass
from enum import StrEnum
import re
from travel_agent.live.observations import text


class ProviderKind(StrEnum):
    GMAIL = "GMAIL"
    OUTLOOK_MAIL = "OUTLOOK_MAIL"
    GOOGLE_CALENDAR = "GOOGLE_CALENDAR"
    OUTLOOK_CALENDAR = "OUTLOOK_CALENDAR"
    FLIGHTAWARE = "FLIGHTAWARE"
    GOOGLE_ROUTES = "GOOGLE_ROUTES"


@dataclass(frozen=True)
class CredentialReference:
    environment_variable: str

    def __post_init__(self):
        if not isinstance(self.environment_variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.environment_variable):
            raise ValueError("Expected environment variable reference, not a credential value")


@dataclass(frozen=True)
class ProviderConfiguration:
    kind: ProviderKind
    account_id: str
    credential: CredentialReference
    enabled: bool = True

    def __post_init__(self):
        if not isinstance(self.kind, ProviderKind) or not isinstance(self.credential, CredentialReference) or type(self.enabled) is not bool:
            raise ValueError("Typed provider configuration required")
        text(self.account_id)


@dataclass(frozen=True)
class SynchronizationPolicy:
    initial_lookback_days: int = 365
    mail_refresh_minutes: int = 1440
    operational_refresh_minutes: int = 240
    near_leave_window_minutes: int = 120
    near_leave_refresh_minutes: int = 30

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value <= 0:
                raise ValueError("Positive integer synchronization policy required")


def validate_connections(connections: tuple[ProviderConfiguration, ...]) -> None:
    keys = [(c.kind, c.account_id) for c in connections]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate provider/account configuration")
