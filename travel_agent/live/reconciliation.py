"""Pure reconciliation of persisted extraction evidence; no V8 snapshot writes."""
from dataclasses import dataclass
from hashlib import sha256
import json

from travel_agent.live.extraction import ExtractionResult


@dataclass(frozen=True)
class ReconciledSegment:
    canonical_id: str
    identity: tuple[str, str, str, str]
    evidence: tuple[ExtractionResult, ...]
    needs_resolution: bool


@dataclass(frozen=True)
class ReconciliationResult:
    segments: tuple[ReconciledSegment, ...]
    unresolved: tuple[ExtractionResult, ...]


def reconcile(results: tuple[ExtractionResult, ...], *, authorized_travelers: frozenset[str]) -> ReconciliationResult:
    """Exact carrier + booking + configured traveler + stable segment reference.

    References are scoped to a booking lifetime; reuse/conflicting facts remain
    unresolved. Different revisions/events are retained, never ordered by mailbox
    receipt time. A later service must resolve authoritative change ordering before
    publishing an active projection. Message identity includes provider and version.
    """
    if not isinstance(authorized_travelers, frozenset):
        raise ValueError("Configured immutable traveler references required")
    messages = {}
    collisions = set()
    for result in results:
        key = result.message.identity
        if key in messages and messages[key] != result:
            collisions.add(key)
        messages.setdefault(key, result)
    groups, unresolved = {}, []
    for result in sorted(set_results(results), key=lambda r: (r.message.identity, repr(r))):
        if result.message.identity in collisions:
            unresolved.append(result)
            continue
        segment = result.segment
        if not result.eligible_for_reconciliation:
            if result.state == "UNRESOLVED":
                unresolved.append(result)
            continue
        if (not segment.booking_reference or not segment.segment_reference or
                segment.traveler_reference not in authorized_travelers):
            unresolved.append(result)
            continue
        identity = (segment.carrier, segment.booking_reference, segment.traveler_reference, segment.segment_reference)
        groups.setdefault(identity, []).append(result)
    reconciled = []
    for identity, evidence in sorted(groups.items()):
        canonical = sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        facts = {(r.state, r.segment) for r in evidence}
        reconciled.append(ReconciledSegment(canonical, identity, tuple(evidence), len(facts) > 1))
    return ReconciliationResult(tuple(reconciled), tuple(unresolved))


def set_results(results):
    # Dataclasses are immutable, but custom rules need not return hashable metadata.
    unique = []
    for result in results:
        if result not in unique:
            unique.append(result)
    return unique
