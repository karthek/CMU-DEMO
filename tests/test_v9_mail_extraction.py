from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import unittest

from travel_agent.live.extraction import ExtractionState as State, ItineraryExtractor, NorthstarRule
from travel_agent.live.mail import MailMessage, html_text
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.reconciliation import reconcile
from travel_agent.live.time import parse_instant


NOW = parse_instant("2026-09-11T12:00Z")
FIXTURES = json.loads((Path(__file__).parent / "fixtures/v9_mail/messages.json").read_text())


def message(name="booking", **updates):
    data = dict(account_id="account-1", message_id=name, version="v1",
                sender="Northstar <tickets@northstar.example.test>", received_at=NOW,
                provider="GMAIL", recipients=("traveler@example.test",),
                provenance=Provenance("synthetic-mail-fixture", NOW, RetrievedBy.SIMULATED), **FIXTURES[name])
    data.update(updates)
    return MailMessage(**data)


class MailExtractionTests(unittest.TestCase):
    def setUp(self):
        self.extractor = ItineraryExtractor()

    def test_fixture_states(self):
        for name, state in (("booking", State.BOOKING), ("change", State.CHANGE),
                            ("cancellation", State.CANCELLATION), ("unrelated", State.NOT_TRAVEL),
                            ("incomplete", State.UNRESOLVED), ("conflicting", State.UNRESOLVED), ("html", State.BOOKING)):
            with self.subTest(name=name):
                result = self.extractor.extract(message(name))
                self.assertEqual(result.state, state)
                if state in (State.UNRESOLVED, State.NOT_TRAVEL):
                    self.assertIsNone(result.segment)
                    self.assertFalse(result.eligible_for_reconciliation)

    def test_booking_normalizes_and_preserves_evidence(self):
        mail = message()
        result = self.extractor.extract(mail)
        self.assertEqual(result.segment.scheduled_departure, parse_instant("2026-09-12T13:00Z"))
        self.assertEqual(result.segment.scheduled_arrival, parse_instant("2026-09-12T18:00Z"))
        self.assertEqual(str(result.segment.departure_date), "2026-09-12")
        self.assertEqual(result.segment.origin_timezone, "America/New_York")
        self.assertEqual(result.rule_id, "synthetic-northstar/v1")
        self.assertEqual(result.message, mail)

    def test_missing_optional_time_is_never_guessed(self):
        result = self.extractor.extract(message("cancellation"))
        self.assertIsNone(result.segment.scheduled_departure)
        self.assertIsNone(result.segment.scheduled_arrival)

    def test_text_html_normalization_and_alternative_conflicts(self):
        self.assertEqual(self.extractor.extract(message("html")).segment, self.extractor.extract(message()).segment)
        self.assertEqual(html_text("<div>A&nbsp; B</div><p>C &amp; D</p>"), "A B\nC & D")
        mail = message(text_body=message().text_body.replace("\n", "\r\n"), html_body=FIXTURES["html"]["html_body"])
        self.assertEqual(self.extractor.extract(mail).state, State.BOOKING)
        self.assertEqual(self.extractor.extract(replace(mail, html_body=mail.html_body.replace("NS123", "NS999"))).state, State.UNRESOLVED)

    def test_sender_validation_and_unsupported_template(self):
        for sender in ("tickets@northstar.example.test.evil.test", "tickets@other.example.test"):
            self.assertEqual(self.extractor.extract(message(sender=sender)).issues, ("UNSUPPORTED_SENDER",))
        self.assertEqual(self.extractor.extract(message(subject="Another airline flight confirmation")).state, State.UNRESOLVED)

    def test_malformed_conflicting_dates_airports_and_times(self):
        changes = (("NS123", "XX123"), ("Origin: ATL", "Origin: AAA"),
                   ("Destination: LAX", "Destination: ATL"), ("2026-09-12T09:00-04:00", "2026-09-12T09:00"),
                   ("2026-09-12T09:00-04:00", "2026-09-12T09:00-05:00"),
                   ("Departure date: 2026-09-12", "Departure date: 2026-09-13"),
                   ("Departure date: 2026-09-12", "Departure date: 2026-02-30"),
                   ("Event: BOOKING", "Event: CHANGE"), ("Origin timezone: America/New_York", "Origin timezone: Unknown/Zone"))
        for before, after in changes:
            with self.subTest(after=after):
                result = self.extractor.extract(message(text_body=message().text_body.replace(before, after)))
                self.assertEqual(result.state, State.UNRESOLVED)
                self.assertIsNone(result.segment)

    def test_dst_gap_and_explicit_fold_offsets(self):
        body = message().text_body
        body = "\n".join(line for line in body.splitlines() if not line.startswith(("Arrival:", "Destination timezone:")))
        gap = body.replace("2026-09-12", "2026-03-08").replace("09:00-04:00", "02:30-05:00")
        self.assertEqual(self.extractor.extract(message(text_body=gap)).state, State.UNRESOLVED)
        for offset in ("-04:00", "-05:00"):
            fold = body.replace("2026-09-12", "2026-11-01").replace("09:00-04:00", "01:30" + offset)
            self.assertEqual(self.extractor.extract(message(text_body=fold)).state, State.BOOKING)

    def test_contract_validation_and_legacy_import(self):
        from travel_agent.live.providers import MailMessage as LegacyMailMessage
        self.assertIs(LegacyMailMessage, MailMessage)
        self.assertEqual(message(sender="Tickets <tickets@NORTHSTAR.EXAMPLE.TEST>").sender, "tickets@northstar.example.test")
        for updates in ({"received_at": datetime(2026, 9, 11)}, {"text_body": None},
                        {"recipients": []}, {"sender": "bad"}, {"sender": "a@example.test,b@example.test"},
                        {"subject": "bad\nsubject"}, {"html_body": 3}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                message(**updates)

    def test_provenance_required(self):
        for updates in ({"provider": "UNSPECIFIED"}, {"provenance": None}):
            self.assertEqual(self.extractor.extract(message(**updates)).state, State.UNRESOLVED)

    def test_rules_are_extensible_and_ambiguous_rules_block(self):
        class OtherRule(NorthstarRule):
            rule_id = "another/v1"
        self.assertEqual(ItineraryExtractor((OtherRule(),)).extract(message()).rule_id, "another/v1")
        self.assertEqual(ItineraryExtractor((NorthstarRule(), OtherRule())).extract(message()).issues, ("AMBIGUOUS_TEMPLATE",))
        with self.assertRaises(ValueError):
            ItineraryExtractor((NorthstarRule(), NorthstarRule()))


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, *messages):
        return reconcile(tuple(ItineraryExtractor().extract(m) for m in messages), authorized_travelers=frozenset({"TRAVELER-1", "TRAVELER-2"}))

    def test_duplicate_message_and_cross_provider_preserve_sources(self):
        first = message()
        second = message(provider="OUTLOOK_MAIL", account_id="account-2", message_id="other", thread_id="thread-2")
        result = self.reconcile(first, first, second)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(len(result.segments[0].evidence), 2)
        self.assertFalse(result.segments[0].needs_resolution)
        self.assertEqual(result, self.reconcile(second, first))

    def test_similar_flights_with_different_identity_never_merge(self):
        for before, after in (("ABC123", "ABC124"), ("COUPON-1", "COUPON-2"), ("TRAVELER-1", "TRAVELER-2")):
            result = self.reconcile(message(), message(message_id="other", text_body=message().text_body.replace(before, after)))
            self.assertEqual(len(result.segments), 2)

    def test_missing_stable_identity_or_unknown_traveler_unresolved(self):
        for label in ("Booking reference:", "Segment reference:", "Traveler reference:"):
            body = "\n".join(line for line in message().text_body.splitlines() if not line.startswith(label))
            result = self.reconcile(message(text_body=body))
            self.assertEqual(result.segments, ())
            self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(len(self.reconcile(message(text_body=message().text_body.replace("TRAVELER-1", "UNKNOWN"))).unresolved), 1)

    def test_changes_and_cancellations_retain_identity_without_selecting_current_state(self):
        result = self.reconcile(message(), message("change"), message("cancellation"))
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(len(result.segments[0].evidence), 3)
        self.assertTrue(result.segments[0].needs_resolution)

    def test_message_version_collision_is_unresolved(self):
        result = self.reconcile(message(), message(text_body=message().text_body.replace("NS123", "NS124")))
        self.assertEqual(result.segments, ())
        self.assertEqual(len(result.unresolved), 2)

    def test_versions_and_conflicting_booking_facts_are_preserved(self):
        result = self.reconcile(message(), message(version="v2", text_body=message().text_body.replace("NS123", "NS124")))
        self.assertTrue(result.segments[0].needs_resolution)
        self.assertEqual(len(result.segments[0].evidence), 2)
