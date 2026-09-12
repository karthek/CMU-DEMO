"""Focused offline cancellation-barrier and explicit reinstatement tests."""
from dataclasses import asdict, replace
from itertools import permutations
import unittest

import test_v9_mail_sync as fixtures
from travel_agent.live.booking import booking_event, project
from travel_agent.live.booking_repository import BookingRepository, decode_event, encode_event
from travel_agent.live.extraction import ItineraryExtractor
from travel_agent.live.repository import encode


def reinstated(sequence=4, target=3, **updates):
    original = fixtures.mail("change", sequence, **updates)
    return replace(original, text_body=original.text_body +
        f"\nTravel state: REINSTATED\nReinstates lifetime: LIFE-1"
        f"\nReinstates cancellation sequence: {target}\nReinstates segment reference: COUPON-1")


class ReinstatementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailSyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def sync(self, *messages, **kwargs):
        outcome, _ = self.fixture.sync(*messages, **kwargs)
        self.assertTrue(outcome.success)
        return self.fixture.current()

    def cancelled(self):
        self.sync(fixtures.mail())
        self.sync(fixtures.mail("change", 2))
        current = self.sync(fixtures.mail("cancellation", 3))
        self.assertEqual(current.status, "CANCELLED")
        return current

    def test_normal_booking_and_change_cannot_reactivate(self):
        cancelled = self.cancelled()
        for kind in ("booking", "change"):
            current = self.sync(fixtures.mail(kind, 4, message_id=f"later-{kind}"))
            self.assertEqual(current.status, "CANCELLED" if kind == "booking" else "UNRESOLVED")
        # Both kinds at the same authority conflict; neither can grant validity.
        self.assertEqual(cancelled.current_authority.sequence, 3)

    def test_normal_later_booking_alone_remains_cancelled(self):
        cancelled = self.cancelled()
        self.assertEqual(self.sync(fixtures.mail("booking", 4, message_id="later")), cancelled)

    def test_normal_later_change_alone_remains_cancelled(self):
        cancelled = self.cancelled()
        self.assertEqual(self.sync(fixtures.mail("change", 4, message_id="later")), cancelled)

    def test_valid_reinstatement_same_identity_and_immutable_history(self):
        cancelled = self.cancelled()
        current = self.sync(reinstated(message_id="reinstatement"))
        self.assertEqual(current.status, "BOOKED")
        self.assertEqual(current.segment_id, cancelled.segment_id)
        self.assertEqual(current.current_authority.sequence, 4)
        self.assertEqual(len(self.fixture.repo.history(current.segment_id)), 4)
        self.assertEqual(len(self.fixture.repo.evidence(current.segment_id)), 4)

    def test_older_reinstatement_cannot_cross_a_newer_cancellation(self):
        self.sync(fixtures.mail("cancellation", 3))
        current = self.sync(fixtures.mail("cancellation", 1, message_id="first-cancellation"),
                            reinstated(2, 1, message_id="old-reinstatement"))
        self.assertEqual(current.status, "CANCELLED")
        self.assertEqual(current.current_authority.sequence, 3)

    def test_incomparable_or_missing_target_is_unresolved(self):
        self.cancelled()
        original = reinstated(message_id="incomparable")
        current = self.sync(replace(original, text_body=original.text_body.replace("Reinstates lifetime: LIFE-1", "Reinstates lifetime: LIFE-2")))
        self.assertEqual(current.status, "UNRESOLVED")

    def test_unseen_cancellation_target_cannot_create_active_state(self):
        current = self.sync(reinstated())
        self.assertEqual(current.status, "UNRESOLVED")
        # Out-of-order arrival can subsequently supply the missing proof.
        self.assertEqual(self.sync(fixtures.mail("cancellation", 3)).status, "BOOKED")

    def test_wrong_segment_and_multipart_ambiguity_are_unresolved(self):
        extractor = ItineraryExtractor()
        cancelled = booking_event(extractor.extract(fixtures.mail("cancellation", 3)))
        original = reinstated()
        for incoming in (
            replace(original, text_body=original.text_body.replace("Reinstates segment reference: COUPON-1", "Reinstates segment reference: OTHER")),
            replace(original, html_body="<p>Reinstates cancellation sequence: 2</p>"),
            replace(original, text_body=original.text_body.replace("Travel state: REINSTATED", "Travel state: UNKNOWN")),
            replace(original, text_body=original.text_body.replace("Reinstates lifetime: LIFE-1", "")),
        ):
            event = booking_event(extractor.extract(incoming))
            self.assertEqual(project("s", "b", (cancelled, event)).status, "UNRESOLVED")

    def test_same_authority_cancellation_and_reinstatement_conflict(self):
        self.cancelled()
        current = self.sync(fixtures.mail("cancellation", 4, message_id="second-cancel"), reinstated(message_id="reinstate"))
        self.assertEqual(current.status, "UNRESOLVED")

    def test_equivalent_provider_copies_and_exact_replay(self):
        self.cancelled()
        incoming = reinstated(message_id="reinstate")
        current = self.sync(incoming)
        before = (self.fixture.count("booking_events"), self.fixture.count("canonical_segment_revisions"))
        self.sync(incoming)
        self.sync(reinstated(provider="OUTLOOK_MAIL", account_id="outlook", message_id="copy"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.assertEqual(self.fixture.current(), current)
        self.assertEqual((self.fixture.count("booking_events"), self.fixture.count("canonical_segment_revisions")), before)
        evidence = [row for row in self.fixture.repo.evidence(current.segment_id) if row["message_id"] in ("reinstate", "copy")]
        self.assertEqual({row["provider"] for row in evidence}, {"GMAIL", "OUTLOOK_MAIL"})
        self.assertEqual(len({row["event_id"] for row in evidence}), 1)

    def test_reopen_preserves_typed_assertion(self):
        self.cancelled()
        current = self.sync(reinstated(message_id="reinstate"))
        self.fixture.repo.close()
        self.fixture.repo = BookingRepository(self.fixture.path, as_of=fixtures.NOW)
        self.assertEqual(self.fixture.current(), current)
        events = [decode_event(row["event_json"]) for row in self.fixture.repo.evidence(current.segment_id)]
        assertion = next(e.reinstatement for e in events if e.reinstatement)
        self.assertEqual(assertion.cancelled_authority.sequence, 3)

    def test_projection_is_independent_of_arrival_order(self):
        extractor = ItineraryExtractor()
        events = tuple(booking_event(extractor.extract(m)) for m in (
            fixtures.mail(), fixtures.mail("cancellation", 3), reinstated(), fixtures.mail("change", 5)))
        projections = [project("s", "b", order) for order in permutations(events)]
        self.assertTrue(all(p == projections[0] for p in projections))
        self.assertEqual(projections[0].status, "BOOKED")

    def test_second_cancellation_requires_a_new_exact_link(self):
        self.cancelled()
        self.sync(reinstated(message_id="first-reinstate"))
        self.sync(fixtures.mail("cancellation", 5, message_id="second-cancel"))
        self.assertEqual(self.sync(fixtures.mail("change", 6, message_id="normal")).status, "CANCELLED")
        self.assertEqual(self.sync(reinstated(7, 5, message_id="second-reinstate")).status, "BOOKED")

    def test_link_to_stale_cancellation_is_not_reinstatement_proof(self):
        self.cancelled()
        self.sync(fixtures.mail("cancellation", 5, message_id="second-cancel"))
        self.assertEqual(self.sync(reinstated(6, 3, message_id="stale-link")).status, "UNRESOLVED")

    def test_v1_ordinary_event_serialization_is_unchanged(self):
        event = booking_event(ItineraryExtractor().extract(fixtures.mail()))
        previous = asdict(event)
        del previous["reinstatement"]
        self.assertEqual(encode_event(event), encode(previous))
        self.assertEqual(decode_event(encode(previous)), event)
