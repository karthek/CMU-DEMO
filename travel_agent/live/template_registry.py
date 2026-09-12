"""Deterministic multi-family parser registry. Registration grants no authority."""
from dataclasses import dataclass
import re

from travel_agent.live.extraction import ExtractedSegment, ExtractionResult, ExtractionRule, ExtractionState
from travel_agent.live.mail import MailMessage


@dataclass(frozen=True)
class TemplateDefinition:
    rule_id: str
    carrier: str | None
    family: str
    event_types: frozenset[ExtractionState]
    parser: ExtractionRule

    def __post_init__(self):
        if not isinstance(self.rule_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*/v[1-9][0-9]*", self.rule_id):
            raise ValueError("Versioned template ID required")
        if not isinstance(self.family, str) or not self.family.strip():
            raise ValueError("Template family required")
        if self.carrier is not None and (not isinstance(self.carrier, str) or not re.fullmatch(r"[A-Z0-9]{2,3}", self.carrier)):
            raise ValueError("Explicit carrier or unknown legacy carrier required")
        if (not isinstance(self.event_types, frozenset) or not self.event_types
                or any(not isinstance(e, ExtractionState) or e not in (
                    ExtractionState.BOOKING, ExtractionState.CHANGE, ExtractionState.CANCELLATION) for e in self.event_types)):
            raise ValueError("Immutable supported extraction events required")
        if (getattr(self.parser, "rule_id", None) != self.rule_id
                or not callable(getattr(self.parser, "matches", None))
                or not callable(getattr(self.parser, "extract", None))):
            raise ValueError("Matching versioned parser required")


@dataclass(frozen=True)
class TemplateRegistry:
    templates: tuple[TemplateDefinition, ...]

    def __post_init__(self):
        if not isinstance(self.templates, tuple) or any(not isinstance(t, TemplateDefinition) for t in self.templates):
            raise ValueError("Immutable template definitions required")
        if len({t.rule_id for t in self.templates}) != len(self.templates):
            raise ValueError("Unique extraction rule identities required")
        object.__setattr__(self, "templates", tuple(sorted(self.templates, key=lambda t: t.rule_id)))

    @classmethod
    def from_rules(cls, rules):
        # Preserve the existing Phase 3 rule injection API. New templates should
        # supply explicit definitions; missing legacy carrier metadata is not trust.
        return cls(tuple(TemplateDefinition(r.rule_id, getattr(r, "carrier", None),
            getattr(r, "family", r.rule_id.rsplit("/", 1)[0]),
            getattr(r, "event_types", frozenset((ExtractionState.BOOKING, ExtractionState.CHANGE, ExtractionState.CANCELLATION))), r)
            for r in rules))

    def extract(self, message: MailMessage) -> ExtractionResult:
        if not isinstance(message, MailMessage):
            raise ValueError("Normalized MailMessage required")
        matches = []
        try:
            for template in self.templates:
                matched = template.parser.matches(message)
                if type(matched) is not bool:
                    raise ValueError("Boolean deterministic match required")
                if matched:
                    matches.append(template)
        except Exception:
            return ExtractionResult(ExtractionState.UNRESOLVED, message, None, issues=("TEMPLATE_MATCH_FAILED",))
        if len(matches) > 1:
            return ExtractionResult(ExtractionState.UNRESOLVED, message, None, issues=("AMBIGUOUS_TEMPLATE",))
        if matches:
            template = matches[0]
            try:
                result = template.parser.extract(message)
                if (not isinstance(result, ExtractionResult) or result.message != message
                        or result.rule_id != template.rule_id or not isinstance(result.state, ExtractionState)
                        or (result.segment is not None and not isinstance(result.segment, ExtractedSegment))
                        or not isinstance(result.issues, tuple) or any(not isinstance(issue, str) for issue in result.issues)
                        or result.state not in template.event_types | {ExtractionState.UNRESOLVED, ExtractionState.NOT_TRAVEL}
                        or (result.segment is not None and template.carrier is not None and result.segment.carrier != template.carrier)):
                    raise ValueError("Parser violated its declared contract")
                return result
            except Exception:
                return ExtractionResult(ExtractionState.UNRESOLVED, message, template.rule_id, issues=("INVALID_TEMPLATE_RESULT",))
        content = "\n".join((message.subject, *message.bodies))
        suspect = re.search(r"\b(flight|itinerary|boarding|airline|booking|reservation)\b", content, re.I)
        state = ExtractionState.UNRESOLVED if suspect else ExtractionState.NOT_TRAVEL
        return ExtractionResult(state, message, None, issues=("UNSUPPORTED_TEMPLATE",) if suspect else ())
