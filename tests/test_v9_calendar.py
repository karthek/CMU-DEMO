"""Bounded calendar semantics only; invented identities, no APIs or personal data."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
import inspect
from zoneinfo import ZoneInfo

import pytest

from travel_agent.live.calendar import (AllDaySpan, CalendarAvailability as Availability,
    CalendarConflictEvaluator, CalendarEventIdentity, CalendarPolicy, CalendarProvider,
    CalendarQuery, CalendarReason as Reason, CalendarScope, CalendarSnapshot,
    ConflictState as State, EventEffect, EventKind, EventStatus, NormalizedCalendarEvent,
    Participation, TimeInterval, TravelerRole)
from travel_agent.live.calendar_agent import CalendarReadAgent, FixtureCalendarProvider
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.providers import ProviderError, ProviderErrorCode as Code, ProviderResult
from travel_agent.live.time import from_local, parse_instant


NOW = parse_instant("2026-09-12T12:00Z")
WINDOW = TimeInterval(parse_instant("2026-09-12T14:00Z"), parse_instant("2026-09-12T16:00Z"))
SCOPE = CalendarScope("FIXTURE", "account-test", "calendar-test")
QUERY = CalendarQuery(SCOPE, WINDOW)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network not permitted in calendar contract tests")
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


def event(eid="event-test", **changes):
    value = NormalizedCalendarEvent(CalendarEventIdentity(SCOPE, eid), WINDOW,
        EventStatus.CONFIRMED, Availability.BUSY, TravelerRole.ATTENDEE, Participation.ACCEPTED,
        Provenance("calendar-fixture", NOW, RetrievedBy.SIMULATED), source_version="revision-test")
    return replace(value, **changes)


def evaluate(*events, policy=CalendarPolicy(), as_of=NOW, window=WINDOW):
    return CalendarConflictEvaluator().evaluate(CalendarSnapshot(QUERY, tuple(events), NOW),
        window, as_of=as_of, policy=policy)


@pytest.mark.parametrize("side", ["start", "end"])
def test_aware_times_required(side):
    with pytest.raises(ValueError):
        replace(WINDOW, **{side: datetime(2026, 9, 12, 14)})


def test_cross_timezone_new_york_los_angeles_overlap():
    period = TimeInterval(datetime(2026, 9, 12, 7, tzinfo=ZoneInfo("America/Los_Angeles")),
                          datetime(2026, 9, 12, 11, tzinfo=ZoneInfo("America/New_York")))
    result = evaluate(event(period=period))
    assert result.state == State.CONFLICT
    assert result.evidence[0].overlap == TimeInterval(WINDOW.start, WINDOW.start + timedelta(hours=1))


@pytest.mark.parametrize("start,end,conflict", [(12, 14, False), (16, 17, False), (14, 16, True)])
def test_half_open_boundaries(start, end, conflict):
    period = TimeInterval(NOW.replace(hour=start), NOW.replace(hour=end))
    assert (evaluate(event(period=period)).state == State.CONFLICT) == conflict


def test_accepted_busy_commitment_conflicts():
    result = evaluate(event())
    assert result.state == State.CONFLICT and result.reliable
    assert result.evidence[0].reason == Reason.BUSY_COMMITMENT


@pytest.mark.parametrize("change,reason", [({"participation": Participation.DECLINED}, Reason.DECLINED),
    ({"status": EventStatus.CANCELLED, "period": None}, Reason.CANCELLED),
    ({"availability": Availability.FREE}, Reason.FREE)])
def test_explicit_nonblocking_evidence(change, reason):
    result = evaluate(event(**change))
    assert result.state == State.NO_CONFLICT and result.evidence[0].reason == reason


@pytest.mark.parametrize("change", [{"status": EventStatus.TENTATIVE},
    {"participation": Participation.TENTATIVE}, {"availability": Availability.TENTATIVE}])
def test_tentative_default_blocks(change):
    result = evaluate(event(**change))
    assert result.state == State.CONFLICT and result.evidence[0].reason == Reason.TENTATIVE_COMMITMENT


def test_explicit_tentative_policy_can_ignore():
    result = evaluate(event(status=EventStatus.TENTATIVE), policy=CalendarPolicy(tentative_blocks=False))
    assert result.state == State.NO_CONFLICT and result.evidence[0].reason == Reason.TENTATIVE_IGNORED


def all_day(zone="America/New_York", **changes):
    return event(period=AllDaySpan(date(2026, 9, 12), date(2026, 9, 13), zone),
                 source_timezone=zone, **changes)


def test_busy_all_day_is_explicit_calendar_constraint():
    value = all_day()
    assert value.all_day and isinstance(value.period.start_date, date)
    result = evaluate(value)
    assert result.state == State.CONFLICT and result.evidence[0].reason == Reason.ALL_DAY_BUSY
    assert result.evidence[0].overlap == WINDOW


def test_free_all_day_does_not_block_airport_travel():
    assert evaluate(all_day(availability=Availability.FREE)).state == State.NO_CONFLICT


def test_unknown_all_day_zone_is_not_midnight_meeting():
    result = evaluate(all_day(None))
    assert result.state == State.UNKNOWN and not result.reliable
    assert result.evidence[0].reason == Reason.UNKNOWN_TIME


def test_unknown_all_day_availability_stays_uncertain():
    result = evaluate(all_day(availability=Availability.UNKNOWN))
    assert result.state == State.UNKNOWN and result.evidence[0].reason == Reason.UNKNOWN_AVAILABILITY


def test_explicit_all_day_policy_ignores_known_busy_span():
    result = evaluate(all_day(), policy=CalendarPolicy(all_day_blocks_when_busy=False))
    assert result.state == State.NO_CONFLICT and result.evidence[0].reason == Reason.ALL_DAY_IGNORED


def occurrence(kind=EventKind.OCCURRENCE, **changes):
    return replace(CalendarEventIdentity(SCOPE, "instance-test", kind, "series-test", "original-slot-test"), **changes)


def test_modified_occurrence_keeps_original_slot_identity():
    first = occurrence()
    modified = occurrence(EventKind.EXCEPTION, event_id="exception-native-id")
    assert first.key == modified.key
    assert replace(first, occurrence_id="other-slot").key != first.key
    assert replace(event(identity=first), period=TimeInterval(WINDOW.start + timedelta(minutes=5), WINDOW.end)).identity.key == first.key


def test_incomplete_recurrence_identity_rejected():
    with pytest.raises(ValueError):
        occurrence(occurrence_id=None)
    with pytest.raises(ValueError):
        CalendarEventIdentity(SCOPE, "single", series_id="inferred")


def test_two_versions_of_one_occurrence_are_not_last_writer_wins():
    with pytest.raises(ValueError):
        CalendarSnapshot(QUERY, (event(identity=occurrence()),
            event(identity=occurrence(EventKind.EXCEPTION), source_version="other")), NOW)


def test_series_master_cannot_masquerade_as_complete_expansion():
    master = event(identity=CalendarEventIdentity(SCOPE, "series", EventKind.SERIES_MASTER))
    result = FixtureCalendarProvider(SCOPE, (master,), NOW).read_events(QUERY)
    assert result.error.code == Code.INVALID_RESPONSE


def test_cancelled_occurrence_does_not_cancel_sibling():
    cancelled = event(identity=occurrence(), status=EventStatus.CANCELLED, period=None)
    sibling = event(identity=occurrence(occurrence_id="another-slot"))
    result = evaluate(cancelled, sibling)
    assert result.state == State.CONFLICT and len(result.conflict_tokens) == 1
    assert {a.effect for a in result.evidence} == {EventEffect.IGNORED, EventEffect.BLOCKING}


def test_organizer_identity_preserved_without_write_authorization():
    organizer = event(traveler_role=TravelerRole.ORGANIZER, participation=Participation.NOT_APPLICABLE,
                      organizer_id="organizer-principal-test")
    result = evaluate(organizer)
    assert result.state == State.CONFLICT
    assert result.evidence[0].event.organizer_id == "organizer-principal-test"
    assert not hasattr(organizer, "can_cancel") and not hasattr(organizer, "authority")


@pytest.mark.parametrize("changes", [{"participation": Participation.UNKNOWN},
    {"participation": Participation.NEEDS_ACTION}, {"traveler_role": TravelerRole.UNKNOWN}])
def test_unknown_participation_never_clears_window(changes):
    result = evaluate(event(**changes))
    assert result.state == State.UNKNOWN and not result.reliable
    assert result.evidence[0].reason == Reason.UNKNOWN_PARTICIPATION


def test_multiple_events_have_deterministic_order():
    a, b = event("a"), event("b")
    assert evaluate(b, a) == evaluate(a, b)
    assert len(evaluate(a, b).conflict_tokens) == 2


def test_empty_complete_snapshot_means_no_conflict():
    result = evaluate()
    assert result.state == State.NO_CONFLICT and result.reliable and result.evidence == ()


def test_bounded_positive_query_contract():
    assert CalendarQuery(SCOPE, TimeInterval(NOW, NOW + timedelta(days=31)))
    with pytest.raises(ValueError):
        CalendarQuery(SCOPE, TimeInterval(NOW, NOW + timedelta(days=31, seconds=1)))
    with pytest.raises(ValueError):
        CalendarQuery(SCOPE, WINDOW, max_events=1001)
    with pytest.raises(ValueError):
        TimeInterval(NOW, NOW)


def test_scope_and_account_isolation():
    other = replace(SCOPE, account_id="other-account")
    assert CalendarEventIdentity(other, "event-test").key != event().identity.key
    with pytest.raises(ValueError):
        CalendarSnapshot(CalendarQuery(other, WINDOW), (event(),), NOW)
    assert FixtureCalendarProvider(SCOPE, (), NOW).read_events(CalendarQuery(other, WINDOW)).error


def test_same_neutral_facts_work_for_future_providers():
    states = []
    for provider in ("GOOGLE_CALENDAR", "OUTLOOK_CALENDAR", "FIXTURE"):
        scope = replace(SCOPE, provider=provider)
        value = event(identity=CalendarEventIdentity(scope, "native-event-test"))
        query = CalendarQuery(scope, WINDOW)
        fixture = FixtureCalendarProvider(scope, (value,), NOW)
        result = CalendarReadAgent(fixture).evaluate_window(query, WINDOW, as_of=NOW)
        states.append(result.value.state)
    assert states == [State.CONFLICT] * 3


def test_structured_conflict_provenance_and_semantic_change_token():
    first = evaluate(event())
    audit_only = evaluate(event(title="Renamed fixture", source_version="new-version"))
    changed = evaluate(event(period=TimeInterval(WINDOW.start, WINDOW.end + timedelta(minutes=10))))
    assert first.conflict_tokens == audit_only.conflict_tokens
    assert first.conflict_tokens != changed.conflict_tokens
    assert first.evidence[0].event.source_version == "revision-test"
    assert first.evidence[0].event.provenance.source == "calendar-fixture"


def test_fixture_result_bound_never_truncates_to_success():
    provider = FixtureCalendarProvider(SCOPE, (event("a"), event("b")), NOW)
    assert provider.read_events(replace(QUERY, max_events=1)).error.code == Code.INVALID_RESPONSE
    with pytest.raises(FrozenInstanceError):
        provider.events = ()


def test_fixture_filters_overlap_not_only_start_and_retains_unknowns():
    overnight = event("overnight", period=TimeInterval(NOW - timedelta(days=1), WINDOW.start + timedelta(minutes=1)))
    later = event("later", period=TimeInterval(WINDOW.end, WINDOW.end + timedelta(hours=1)))
    uncertain = event("uncertain", period=None)
    result = FixtureCalendarProvider(SCOPE, (overnight, later, uncertain), NOW).read_events(QUERY)
    assert {e.identity.event_id for e in result.value.events} == {"overnight", "uncertain"}


def test_legacy_calendar_agent_boundary_remains_opt_in_and_unchanged():
    from travel_agent.agents.calendar_agent import CalendarAgent
    from travel_agent.tools.calendar_tool import FakeCalendarTool
    old = CalendarAgent(FakeCalendarTool()).get_events("2026-09-12")
    assert all(datetime.fromisoformat(e.start).tzinfo is None for e in old)
    assert CalendarReadAgent(FixtureCalendarProvider(SCOPE, (), NOW)).evaluate_window(QUERY, WINDOW, as_of=NOW).value.state == State.NO_CONFLICT
    assert set(n for n, m in inspect.getmembers(CalendarAgent, inspect.isfunction) if not n.startswith('_')) == {
        "get_events", "last_meeting_end", "departure_conflicts", "free_windows"}


def test_new_calendar_interfaces_have_no_mutation_methods():
    for kind, expected in ((CalendarProvider, {"read_events"}), (FixtureCalendarProvider, {"read_events"}),
                           (CalendarReadAgent, {"evaluate_window"})):
        public = {n for n, m in inspect.getmembers(kind, inspect.isfunction) if not n.startswith('_')}
        assert public == expected


def test_dst_fold_uses_instants_not_same_zone_wall_clock_order():
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    period = TimeInterval(start, end)
    assert period.end - period.start == timedelta(minutes=30)
    with pytest.raises(ValueError):
        from_local(datetime(2026, 11, 1, 1, 30), "America/New_York")


def test_dst_gap_is_not_silently_repaired():
    with pytest.raises(ValueError):
        TimeInterval(datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
                     datetime(2026, 3, 8, 4, tzinfo=ZoneInfo("America/Los_Angeles")))


def test_all_day_dst_span_is_23_hours_and_end_exclusive():
    span = AllDaySpan(date(2026, 3, 8), date(2026, 3, 9), "America/New_York")
    interval = span.interval()
    assert interval.end - interval.start == timedelta(hours=23)
    assert interval.overlap(TimeInterval(interval.end, interval.end + timedelta(hours=1))) is None


def test_inadequate_coverage_rejected_before_provider_call():
    class NeverCalled:
        def read_events(self, query):
            pytest.fail("Invalid query reached provider")
    with pytest.raises(ValueError):
        CalendarReadAgent(NeverCalled()).evaluate_window(QUERY, TimeInterval(WINDOW.start, WINDOW.end + timedelta(seconds=1)), as_of=NOW)


def test_provider_failure_preserved_and_not_empty_success():
    class Failed:
        def read_events(self, query):
            return ProviderResult(error=ProviderError(Code.RATE_LIMITED, True, 20))
    result = CalendarReadAgent(Failed()).evaluate_window(QUERY, WINDOW, as_of=NOW)
    assert result.error == ProviderError(Code.RATE_LIMITED, True, 20) and result.value is None


def test_wrong_query_response_is_neutral_failure():
    class Wrong:
        def read_events(self, query):
            return ProviderResult(value=CalendarSnapshot(replace(query, max_events=1), (), NOW))
    result = CalendarReadAgent(Wrong()).evaluate_window(QUERY, WINDOW, as_of=NOW)
    assert result.error.code == Code.INVALID_RESPONSE


def test_freshness_boundary_and_future_evidence_are_conservative():
    assert evaluate(as_of=NOW + timedelta(minutes=15)).state == State.NO_CONFLICT
    assert evaluate(as_of=NOW + timedelta(minutes=15, microseconds=1)).state == State.UNKNOWN
    assert evaluate(as_of=NOW - timedelta(seconds=1)).state == State.UNKNOWN


def test_known_conflict_and_unknown_evidence_cannot_claim_reliability():
    result = evaluate(event("known"), event("unknown", status=EventStatus.REMOVED, period=None))
    assert result.state == State.CONFLICT and not result.reliable
    assert len(result.conflict_tokens) == 1
    assert Reason.REMOVAL_UNRESOLVED in {e.reason for e in result.evidence}


def test_typed_status_and_all_day_zone_consistency_required():
    with pytest.raises(ValueError):
        event(status="confirmed")
    with pytest.raises(ValueError):
        replace(all_day(), source_timezone="America/Los_Angeles")


def test_fixture_cannot_hide_conflicting_version_outside_query():
    inside = event()
    outside = replace(inside, source_version="other", period=TimeInterval(WINDOW.end, WINDOW.end + timedelta(hours=1)))
    with pytest.raises(ValueError):
        FixtureCalendarProvider(SCOPE, (inside, outside), NOW)
