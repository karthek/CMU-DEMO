"""Offline framework proofs using explicitly fictional families, never airline samples."""
from contextlib import closing
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import timedelta
import hashlib
from itertools import permutations
import json
from pathlib import Path

import pytest

from test_v9_mail_sync import mail, NOW, TRAVELERS, OfflineMailSource, page
from test_v9_reinstatement import reinstated
from travel_agent.live.booking import booking_event, planning_readiness
from travel_agent.live.booking_repository import BookingRepository, encode_event
from travel_agent.live.extraction import ExtractionResult, ExtractionState as State, ItineraryExtractor, NorthstarRule
from travel_agent.live.mail_sync import MailSynchronization
from travel_agent.live.template_authority import (
    DEFAULT_TEMPLATE_EVENT_POLICY, TRAVEL_EVENTS, TemplateEventGrant, TemplateEventAuthorityPolicy,
)
from travel_agent.live.template_events import northstar_event_v1
from travel_agent.live.template_registry import TemplateDefinition, TemplateRegistry


@dataclass(frozen=True)
class FictionalRule:
    """Distinct fictional envelopes around the existing synthetic field grammar."""
    family: str = "fixture-alpha"
    carrier: str = "XA"
    version: int = 1

    @property
    def rule_id(self):
        return f"{self.family}/v{self.version}"

    def matches(self, message):
        return message.subject in {f"{self.family} {event.value}" for event in TRAVEL_EVENTS}

    def extract(self, message):
        if (message.sender != f"tickets@{self.family}.example.test"
                or not all(f"Template: {self.family}" in body.splitlines() for body in message.bodies)):
            return ExtractionResult(State.UNRESOLVED, message, self.rule_id, issues=("UNSUPPORTED_FIXTURE",))
        kind = State(message.subject.split()[-1])
        subject = next(s for s, state in NorthstarRule.subjects.items() if state == kind)
        normalized = replace(message, sender="tickets@northstar.example.test", subject=subject,
            text_body=message.text_body.replace(f"Carrier: {self.carrier}", "Carrier: NS").replace(f"Flight: {self.carrier}", "Flight: NS"),
            html_body=(message.html_body or "").replace(f"Carrier: {self.carrier}", "Carrier: NS").replace(f"Flight: {self.carrier}", "Flight: NS"))
        result = NorthstarRule().extract(normalized)
        segment = replace(result.segment, carrier=self.carrier,
            flight_number=self.carrier + result.segment.flight_number[2:]) if result.segment else None
        return replace(result, rule_id=self.rule_id, message=message, segment=segment)


def incoming(rule, kind="booking", sequence=1, **kwargs):
    message = mail(kind, sequence, **kwargs)
    return replace(message, sender=f"tickets@{rule.family}.example.test", subject=f"{rule.family} {kind.upper()}",
        text_body=message.text_body.replace("Carrier: NS", f"Carrier: {rule.carrier}")
            .replace("Flight: NS", f"Flight: {rule.carrier}") + f"\nTemplate: {rule.family}")


def definition(rule, events=TRAVEL_EVENTS):
    return TemplateDefinition(rule.rule_id, rule.carrier, rule.family, events, rule)


def extractor(*rules):
    return ItineraryExtractor(registry=TemplateRegistry(tuple(definition(r) for r in rules)))


def policy(rule, events=TRAVEL_EVENTS):
    return TemplateEventAuthorityPolicy("fixture-policy/v1", (
        *DEFAULT_TEMPLATE_EVENT_POLICY.grants,
        TemplateEventGrant(rule.rule_id, rule.carrier, events, northstar_event_v1),
    ))


@pytest.mark.parametrize("case", ["booking", "change", "cancellation", "unsequenced", "reinstatement", "invalid_authority"])
def test_authority_frozen_northstar_event_bytes(case):
    cases = {"booking": mail(), "change": mail("change", 2), "cancellation": mail("cancellation", 3),
             "unsequenced": mail(sequence=None), "reinstatement": reinstated(),
             "invalid_authority": replace(mail(), text_body=mail().text_body.replace("Airline sequence: 1", "Airline sequence: invalid"))}
    hashes = json.loads((Path(__file__).parent / "fixtures/v9_templates/northstar_event_hashes.json").read_text())
    event = booking_event(ItineraryExtractor().extract(cases[case]))
    assert hashlib.sha256(encode_event(event).encode()).hexdigest() == hashes["sha256"][case]


@pytest.mark.parametrize("kind", ["booking", "change", "cancellation"])
def test_authority_unknown_template_denied(kind):
    rule = FictionalRule()
    result = extractor(rule).extract(incoming(rule, kind))
    assert result.state == kind.upper()
    assert DEFAULT_TEMPLATE_EVENT_POLICY.decision(result).reason == "UNREGISTERED_TEMPLATE"
    assert booking_event(result) is None


@pytest.mark.parametrize("kind", ["booking", "change", "cancellation"])
def test_authority_explicit_exact_template_can_emit_events(kind):
    rule = FictionalRule()
    result = extractor(rule).extract(incoming(rule, kind))
    event = booking_event(result, policy=policy(rule))
    assert event.kind == result.state
    assert event.segment == result.segment
    assert event.authority.lifetime == "LIFE-1"


def test_authority_partial_permissions_and_version_isolation():
    v1, v2 = FictionalRule(), FictionalRule(version=2)
    configured = policy(v1, frozenset((State.BOOKING, State.CHANGE)))
    for kind in ("booking", "change"):
        assert configured.decision(extractor(v1).extract(incoming(v1, kind))).permitted
    assert configured.decision(extractor(v1).extract(incoming(v1, "cancellation"))).reason == "EVENT_NOT_AUTHORIZED"
    assert configured.decision(extractor(v2).extract(incoming(v2))).reason == "UNREGISTERED_TEMPLATE"


def test_authority_configuration_is_immutable_and_explicit():
    configured = policy(FictionalRule())
    with pytest.raises(FrozenInstanceError):
        configured.grants = ()
    with pytest.raises(FrozenInstanceError):
        configured.grants[0].event_types = TRAVEL_EVENTS
    for args in (("unversioned", ()), ("policy/v1", list(configured.grants)),
                 ("policy/v1", configured.grants * 2)):
        with pytest.raises(ValueError):
            TemplateEventAuthorityPolicy(*args)
    with pytest.raises(ValueError):
        TemplateEventGrant("fixture/v1", "XA", frozenset((State.UNRESOLVED,)), northstar_event_v1)


def test_authority_cannot_change_parser_facts_or_ignore_issues():
    rule = FictionalRule()
    result = extractor(rule).extract(incoming(rule))
    bad = TemplateEventAuthorityPolicy("bad-policy/v1", (TemplateEventGrant(rule.rule_id, rule.carrier,
        TRAVEL_EVENTS, lambda r: replace(northstar_event_v1(r), kind=State.CANCELLATION)),))
    with pytest.raises(ValueError):
        bad.construct(result)
    assert policy(rule).decision(replace(result, issues=("CONFLICTING_FIELDS",))).reason == "EXTRACTION_ISSUES"
    assert policy(rule).decision(replace(result, segment=replace(result.segment, carrier="XB"))).reason == "CARRIER_MISMATCH"


@pytest.mark.parametrize("provider", ["GMAIL", "OUTLOOK_MAIL", "FUTURE_MAIL"])
def test_registry_and_authority_are_provider_neutral(provider):
    rule = FictionalRule()
    first = extractor(rule).extract(incoming(rule))
    other = extractor(rule).extract(incoming(rule, provider=provider, account_id="other"))
    assert first.segment == other.segment
    assert policy(rule).decision(first) == policy(rule).decision(other)
    assert policy(rule).construct(first) == policy(rule).construct(other)


def test_registry_multiple_families_selection_is_order_independent():
    rules = (FictionalRule(), FictionalRule("fixture-beta", "XB"), FictionalRule("fixture-gamma", "XC"))
    for ordering in permutations(rules):
        registry = extractor(*ordering)
        for rule in rules:
            result = registry.extract(incoming(rule))
            assert result.rule_id == rule.rule_id
            assert result.segment.carrier == rule.carrier
            assert result.state == State.BOOKING
            assert sum(r.matches(incoming(rule)) for r in rules) == 1


@pytest.mark.parametrize("mutation", ["sender", "subject", "body", "flight", "airport", "date", "conflict"])
def test_registry_unsupported_fictional_evidence_fails_closed(mutation):
    rule = FictionalRule()
    message = incoming(rule)
    mutations = {
        "sender": {"sender": "tickets@fixture-beta.example.test"},
        "subject": {"subject": "Unknown flight layout"},
        "body": {"text_body": "Flight booking incomplete"},
        "flight": {"text_body": message.text_body.replace("Flight: XA123", "Flight: BROKEN")},
        "airport": {"text_body": message.text_body.replace("Origin: ATL", "Origin: INVALID")},
        "date": {"text_body": message.text_body.replace("Departure date: 2026-09-12", "Departure date: invalid")},
        "conflict": {"text_body": message.text_body + "\nFlight: XA999"},
    }
    result = extractor(rule).extract(replace(message, **mutations[mutation]))
    assert result.state == State.UNRESOLVED
    assert policy(rule).construct(result) is None


def test_registry_no_match_keeps_not_travel_distinction():
    rule = FictionalRule()
    result = extractor(rule).extract(replace(incoming(rule), subject="Monthly offers", text_body="Save on merchandise"))
    assert result.state == State.NOT_TRAVEL


def test_registry_overlapping_versions_fail_closed_regardless_of_authority():
    v1, v2 = FictionalRule(), FictionalRule(version=2)
    for rules in ((v1, v2), (v2, v1)):
        result = extractor(*rules).extract(incoming(v1))
        assert result.state == State.UNRESOLVED
        assert result.issues == ("AMBIGUOUS_TEMPLATE",)
        assert policy(v1).construct(result) is None


def test_registry_rejects_duplicate_ids_and_undeclared_event():
    rule = FictionalRule()
    with pytest.raises(ValueError):
        TemplateRegistry((definition(rule), definition(rule)))
    registry = TemplateRegistry((definition(rule, frozenset((State.BOOKING,))),))
    assert registry.extract(incoming(rule, "cancellation")).issues == ("INVALID_TEMPLATE_RESULT",)


@pytest.mark.parametrize("fault", ["rule_id", "message", "carrier", "exception", "match_exception", "match_non_bool"])
def test_registry_rejects_parser_contract_violations(fault):
    class BadRule(FictionalRule):
        def matches(self, message):
            if fault == "match_exception":
                raise RuntimeError("private parser diagnostic")
            return 1 if fault == "match_non_bool" else True

        def extract(self, message):
            result = super().extract(message)
            if fault == "exception":
                raise RuntimeError("private parser diagnostic")
            if fault == "rule_id":
                return replace(result, rule_id="synthetic-northstar/v1")
            if fault == "message":
                return replace(result, message=replace(message, message_id="forged"))
            return replace(result, segment=replace(result.segment, carrier="XB"))

    rule = BadRule()
    result = extractor(rule).extract(incoming(rule))
    assert result.state == State.UNRESOLVED
    assert result.segment is None
    assert "private" not in str(result.issues)


def run_sync(repo, engine, messages, tick=1):
    provider, account_id = messages[0].provider, messages[0].account_id
    service = MailSynchronization(repo, authorized_travelers=TRAVELERS, extractor=engine)
    outcome = service.run(OfflineMailSource(page(messages, f"cursor-{tick}")), provider, account_id,
        as_of=NOW + timedelta(minutes=tick))
    assert outcome.success, outcome.error


@pytest.mark.parametrize("kind", ["booking", "change", "cancellation"])
def test_authority_unregistered_cannot_create_canonical_rows(tmp_path, kind):
    rule = FictionalRule()
    with closing(BookingRepository(tmp_path / "state.sqlite3", as_of=NOW)) as repo:
        run_sync(repo, extractor(rule), (incoming(rule, kind),))
        assert len(repo.extractions()) == 1
        assert not repo.bookings()
        assert not repo.current_segments()
        assert repo.template_authority_decisions()[0][1].reason == "UNREGISTERED_TEMPLATE"


@pytest.mark.parametrize("kind", ["change", "cancellation"])
def test_authority_unknown_mutation_cannot_change_existing_state(tmp_path, kind):
    rule = FictionalRule(carrier="NS")
    with closing(BookingRepository(tmp_path / "state.sqlite3", as_of=NOW)) as repo:
        run_sync(repo, ItineraryExtractor(), (mail(),))
        original = repo.current_segments()
        run_sync(repo, extractor(rule), (incoming(rule, kind, 2),), 2)
        assert repo.current_segments() == original
        assert len(repo.history(original[0].segment_id)) == 1
        assert len(repo.extractions()) == 2


def test_authority_reviewed_second_family_full_flow_replay_and_restart(tmp_path):
    rule = FictionalRule()
    path = tmp_path / "state.sqlite3"
    engine = extractor(FictionalRule("fixture-beta", "XB"), rule)
    configured = policy(rule)
    with closing(BookingRepository(path, as_of=NOW, event_policy=configured)) as repo:
        run_sync(repo, engine, (incoming(rule),))
        original = repo.current_segments()[0]
        assert planning_readiness(original, authorized_travelers=TRAVELERS).planning_allowed
        run_sync(repo, engine, (incoming(rule, "change", 2),), 2)
        changed = repo.current_segments()[0]
        assert changed.segment_id == original.segment_id
        assert changed.schedule != original.schedule
        run_sync(repo, engine, (incoming(rule, "cancellation", 3),), 3)
        cancelled = repo.current_segments()[0]
        assert cancelled.status == "CANCELLED"
        assert not planning_readiness(cancelled, authorized_travelers=TRAVELERS).planning_allowed
        assert len(repo.history(original.segment_id)) == 3
        assert len(repo.evidence(original.segment_id)) == 3
        decisions = repo.template_authority_decisions()
        assert all(d.permitted and d.policy_id == configured.policy_id for _, d in decisions)
    with closing(BookingRepository(path, as_of=NOW, event_policy=configured)) as repo:
        assert repo.template_authority_decisions() == decisions
        run_sync(repo, engine, (incoming(rule), incoming(rule, "change", 2), incoming(rule, "cancellation", 3)), 4)
        assert repo.current_segments() == (cancelled,)
        assert len(repo.history(cancelled.segment_id)) == 3
        assert repo.connection.execute("SELECT count(*) FROM booking_events").fetchone()[0] == 3
        run_sync(repo, engine, (incoming(rule, "booking", 4, message_id="later"),), 5)
        assert repo.current_segments()[0].status == "CANCELLED"


def test_authority_partial_cancellation_retained_without_mutation(tmp_path):
    rule = FictionalRule()
    configured = policy(rule, frozenset((State.BOOKING, State.CHANGE)))
    with closing(BookingRepository(tmp_path / "state.sqlite3", as_of=NOW, event_policy=configured)) as repo:
        run_sync(repo, extractor(rule), (incoming(rule),))
        original = repo.current_segments()
        run_sync(repo, extractor(rule), (incoming(rule, "cancellation", 2),), 2)
        assert repo.current_segments() == original
        assert len(repo.extractions()) == 2
        assert any(d.reason == "EVENT_NOT_AUTHORIZED" for _, d in repo.template_authority_decisions())
