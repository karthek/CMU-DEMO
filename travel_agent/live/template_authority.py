"""Immutable, deployment-owned exact-template/event permissions, not mail trust."""
from collections.abc import Callable
from dataclasses import dataclass
import re

from travel_agent.live.booking import BookingEvent
from travel_agent.live.extraction import ExtractionResult, ExtractionState
from travel_agent.live.template_events import northstar_event_v1


TRAVEL_EVENTS = frozenset((ExtractionState.BOOKING, ExtractionState.CHANGE, ExtractionState.CANCELLATION))


def versioned_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*/v[1-9][0-9]*", value):
        raise ValueError("Exact versioned template/policy ID required")


@dataclass(frozen=True)
class TemplateEventGrant:
    rule_id: str
    carrier: str
    event_types: frozenset[ExtractionState]
    event_builder: Callable[[ExtractionResult], BookingEvent]

    def __post_init__(self):
        versioned_id(self.rule_id)
        if not isinstance(self.carrier, str) or not re.fullmatch(r"[A-Z0-9]{2,3}", self.carrier):
            raise ValueError("Explicit carrier required")
        if (not isinstance(self.event_types, frozenset) or not self.event_types
                or any(not isinstance(e, ExtractionState) or e not in TRAVEL_EVENTS for e in self.event_types)
                or not callable(self.event_builder)):
            raise ValueError("Explicit immutable event permissions and builder required")


@dataclass(frozen=True)
class TemplateAuthorityDecision:
    policy_id: str
    rule_id: str | None
    event_type: ExtractionState
    permitted: bool
    reason: str


@dataclass(frozen=True)
class TemplateEventAuthorityPolicy:
    policy_id: str
    grants: tuple[TemplateEventGrant, ...]

    def __post_init__(self):
        versioned_id(self.policy_id)
        if not isinstance(self.grants, tuple) or any(not isinstance(g, TemplateEventGrant) for g in self.grants):
            raise ValueError("Immutable template grants required")
        if len({g.rule_id for g in self.grants}) != len(self.grants):
            raise ValueError("One explicit grant per exact template version")
        object.__setattr__(self, "grants", tuple(sorted(self.grants, key=lambda g: g.rule_id)))

    def decision(self, result: ExtractionResult) -> TemplateAuthorityDecision:
        if not isinstance(result, ExtractionResult) or not isinstance(result.state, ExtractionState):
            raise ValueError("Typed extraction required")
        grant = next((g for g in self.grants if g.rule_id == result.rule_id), None)
        reason = ("NOT_ELIGIBLE" if not result.eligible_for_reconciliation else
                  "EXTRACTION_ISSUES" if result.issues else
                  "UNREGISTERED_TEMPLATE" if grant is None else
                  "EVENT_NOT_AUTHORIZED" if result.state not in grant.event_types else
                  "CARRIER_MISMATCH" if result.segment.carrier != grant.carrier else "AUTHORIZED")
        return TemplateAuthorityDecision(self.policy_id, result.rule_id, result.state,
                                         reason == "AUTHORIZED", reason)

    def construct(self, result: ExtractionResult) -> BookingEvent | None:
        if not self.decision(result).permitted:
            return None
        grant = next(g for g in self.grants if g.rule_id == result.rule_id)
        event = grant.event_builder(result)
        # Trusted interpreter may add document order/transition evidence, never
        # rewrite the parser's identity, facts or event classification.
        if (not isinstance(event, BookingEvent) or event.kind != result.state or event.segment != result.segment
                or not isinstance(event.parser_version, str) or not event.parser_version):
            raise ValueError("Event interpreter changed extracted facts")
        return event


DEFAULT_TEMPLATE_EVENT_POLICY = TemplateEventAuthorityPolicy("template-authority/v1", (
    TemplateEventGrant("synthetic-northstar/v1", "NS", TRAVEL_EVENTS, northstar_event_v1),
))
