from dataclasses import replace
import unittest
from travel_agent.live.config import (CredentialReference, ProviderConfiguration, ProviderKind, SynchronizationPolicy, validate_connections)
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.providers import (CalendarAuthority, CalendarEvent, FlightIdentity, FlightObservation, MailMessage, MailSyncPage,
    ProviderError, ProviderErrorCode, ProviderResult, TrafficEstimate, TrafficRequest)
from travel_agent.live.time import parse_instant


class V9ProviderTests(unittest.TestCase):
    def test_typed_errors_and_permission_flags(self):
        with self.assertRaises(ValueError):
            ProviderError("UNAVAILABLE", True)
        with self.assertRaises(ValueError):
            ProviderError(ProviderErrorCode.RATE_LIMITED, True, True)
        with self.assertRaises(ValueError):
            ProviderResult(error="failed")
        with self.assertRaises(ValueError):
            CalendarAuthority(False, "yes", False, False)

    def test_offline_fakes_for_all_provider_ports(self):
        now = parse_instant("2026-09-11T12:00Z")
        provenance = Provenance("test-only", now, RetrievedBy.SIMULATED)
        identity = FlightIdentity("s1", "AA123", "ATL", "PHL", now, "America/New_York")
        message = MailMessage("a1", "m1", "v1", "airline@example.test", "Flight", "test", now)
        event = CalendarEvent("a1", "c1", "e1", "v1", now, parse_instant("2026-09-11T13:00Z"), CalendarAuthority(False, False, False, True))
        class FakeMail:
            def sync(self, *, since, cursor, page_token=None):
                return ProviderResult(MailSyncPage((message,), (), None, "opaque-completed-cursor"))
        class FakeFlight:
            def get_flight(self, identity):
                return ProviderResult(FlightObservation(identity, now, False, None, None, provenance))
        class FakeCalendar:
            def list_events(self, *, start, end):
                return ProviderResult((event,))
            def get_event(self, calendar_id, event_id):
                return ProviderResult(event)
        class FakeTraffic:
            def estimate(self, request):
                return ProviderResult(TrafficEstimate(request, 40, provenance))
        self.assertEqual(FakeMail().sync(since=now, cursor=None).value.messages, (message,))
        self.assertIsNone(FakeFlight().get_flight(identity).value.gate)
        self.assertEqual(FakeCalendar().get_event("c1", "e1").value.authority.can_decline, True)
        self.assertEqual(len(FakeCalendar().list_events(start=now, end=event.end).value), 1)
        request = TrafficRequest("HOME", "ATL", parse_instant("2026-09-12T12:00Z"))
        self.assertEqual(FakeTraffic().estimate(request).value.request.departure_time, request.departure_time)

    def test_error_xor_and_cursor_commit_boundary(self):
        error = ProviderError(ProviderErrorCode.CURSOR_EXPIRED, True)
        self.assertEqual(ProviderResult(error=error).error, error)
        with self.assertRaises(ValueError):
            ProviderResult()
        with self.assertRaises(ValueError):
            ProviderResult(value=(), error=error)
        with self.assertRaises(ValueError):
            MailSyncPage((), (), "next-page", "premature-cursor")

    def test_multiple_accounts_and_configuration_no_secret_loading(self):
        connections = tuple(ProviderConfiguration(kind, "account", CredentialReference(kind.value + "_TOKEN")) for kind in ProviderKind)
        validate_connections(connections)
        self.assertEqual(len(connections), 6)
        with self.assertRaises(ValueError):
            validate_connections(connections + connections[:1])
        with self.assertRaises(ValueError):
            CredentialReference("secret-value-not-an-env-name")
        self.assertEqual(SynchronizationPolicy().initial_lookback_days, 365)
        self.assertEqual(SynchronizationPolicy().near_leave_refresh_minutes, 30)
        with self.assertRaises(ValueError):
            SynchronizationPolicy(mail_refresh_minutes=True)
