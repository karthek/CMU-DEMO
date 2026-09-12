"""Booking-wide lifetime collisions: synthetic evidence, stable keys, no reissue inference."""
from dataclasses import replace
from itertools import permutations
import unittest
from unittest.mock import patch

import test_v9_mail_sync as fixtures
from test_v9_reinstatement import reinstated
from travel_agent.live.booking import (LifetimeStatus, booking_lifetime_compatibility,
    booking_event, constrain_booking_lifetime, planning_readiness, project)
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.extraction import ItineraryExtractor


def mail(lifetime="LIFE-1", coupon="COUPON-1", **kwargs):
    original = fixtures.mail(**kwargs)
    return replace(original, text_body=original.text_body.replace("LIFE-1", lifetime).replace("COUPON-1", coupon))


class BookingLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailSyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def sync(self, *messages, **kwargs):
        outcome, _ = self.fixture.sync(*messages, **kwargs)
        self.assertTrue(outcome.success)
        return self.fixture.repo.current_segments()

    def booking(self):
        bookings = self.fixture.repo.bookings()
        self.assertEqual(len(bookings), 1)
        return bookings[0]

    def assert_collision(self, segments):
        self.assertEqual(self.booking().lifetime_compatibility.status, LifetimeStatus.UNRESOLVED)
        self.assertEqual(self.booking().lifetime_compatibility.lifetimes, ("LIFE-1", "LIFE-2"))
        for segment in segments:
            self.assertIn(segment.status, ("UNRESOLVED", "CANCELLED"))
            self.assertFalse(planning_readiness(segment, authorized_travelers=fixtures.TRAVELERS).planning_allowed)

    def test_same_lifetime_two_coupons_one_booking(self):
        segments = self.sync(mail(), mail(coupon="COUPON-2", message_id="second"))
        self.assertEqual(len(segments), 2)
        self.assertEqual(len({s.segment_id for s in segments}), 2)
        self.assertEqual(self.booking().lifetime_compatibility.status, LifetimeStatus.COMPATIBLE)
        self.assertTrue(all(s.status == "BOOKED" for s in segments))

    def test_same_lifetime_same_coupon_preserves_ids(self):
        original = self.sync(mail())[0]
        booking_id = self.booking().booking_id
        segments = self.sync(mail(message_id="copy"))
        self.assertEqual(segments, (original,))
        self.assertEqual(self.booking().booking_id, booking_id)
        self.assertEqual(self.fixture.count("booking_events"), 1)

    def test_different_lifetimes_same_coupon_unresolved(self):
        original = self.sync(mail())[0]
        segments = self.sync(mail("LIFE-2", message_id="later-lifetime"))
        self.assert_collision(segments)
        self.assertEqual(segments[0].segment_id, original.segment_id)
        self.assertIsNone(segments[0].schedule)
        self.assertEqual(len(self.fixture.repo.evidence(original.segment_id)), 2)

    def test_different_lifetimes_different_coupons_block_entire_booking(self):
        original = self.sync(mail())[0]
        segments = self.sync(mail("LIFE-2", "COUPON-2", message_id="later-lifetime"))
        self.assert_collision(segments)
        self.assertEqual(len(segments), 2)
        self.assertIn(original.segment_id, {s.segment_id for s in segments})
        self.assertTrue(all("BOOKING_LIFETIME_COLLISION" in s.reasons for s in segments))
        self.assertTrue(all(s.schedule is None for s in segments))
        # Both sibling events explain each blocked revision.
        rows = self.fixture.repo.connection.execute("""SELECT c.segment_id,count(r.event_id)
            FROM canonical_current c JOIN revision_events r USING(revision_id) GROUP BY c.segment_id""").fetchall()
        self.assertTrue(all(row[1] == 2 for row in rows))

    def test_cancelled_old_coupon_is_not_mutated_by_new_lifetime(self):
        cancelled = self.sync(mail(kind="cancellation", sequence=3))[0]
        history = self.fixture.repo.history(cancelled.segment_id)
        segments = self.sync(mail("LIFE-2", "COUPON-2", message_id="new-trip"))
        self.assert_collision(segments)
        self.assertEqual(next(s for s in segments if s.segment_id == cancelled.segment_id), cancelled)
        self.assertEqual(self.fixture.repo.history(cancelled.segment_id), history)

    def test_cancelled_same_coupon_history_survives_collision(self):
        cancelled = self.sync(mail(kind="cancellation", sequence=3))[0]
        old_revision = self.fixture.repo.history(cancelled.segment_id)[0]
        segments = self.sync(mail("LIFE-2", message_id="new-trip"))
        self.assert_collision(segments)
        self.assertIn(old_revision, self.fixture.repo.history(cancelled.segment_id))
        self.assertIn('"status":"CANCELLED"', old_revision["projection_json"])

    def test_same_lifetime_cross_provider_dedup_and_replay(self):
        first = mail()
        original = self.sync(first)[0]
        self.sync(mail(provider="OUTLOOK_MAIL", account_id="outlook", message_id="copy"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.sync(first)
        self.assertEqual(self.fixture.repo.current_segments(), (original,))
        self.assertEqual(self.fixture.count("booking_events"), 1)
        self.assertEqual(self.fixture.count("canonical_segment_revisions"), 1)
        self.assertEqual({r["provider"] for r in self.fixture.repo.evidence(original.segment_id)}, {"GMAIL", "OUTLOOK_MAIL"})
        self.assertEqual(self.booking().lifetime_compatibility.status, LifetimeStatus.COMPATIBLE)

    def test_cross_provider_different_lifetimes_no_merge(self):
        self.sync(mail())
        segments = self.sync(mail("LIFE-2", "COUPON-2", provider="OUTLOOK_MAIL", account_id="outlook"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.assert_collision(segments)
        self.assertEqual(self.fixture.count("mail_messages"), 2)

    def test_batch_arrival_permutations_have_identical_current_results(self):
        incoming = (mail(kind="cancellation", sequence=3), mail("LIFE-2", "COUPON-2", message_id="other"))
        snapshots = []
        for index, order in enumerate(permutations(incoming)):
            path = self.fixture.path.parent / f"permutation-{index}.sqlite3"
            repo = BookingRepository(path, as_of=fixtures.NOW)
            try:
                service = fixtures.MailSynchronization(repo, authorized_travelers=fixtures.TRAVELERS)
                for number, message in enumerate(order):
                    source = fixtures.OfflineMailSource(fixtures.page((message,), cursor=str(number)))
                    self.assertTrue(service.run(source, "GMAIL", "account-1", as_of=fixtures.NOW).success)
                snapshots.append((repo.bookings(), repo.current_segments()))
            finally:
                repo.close()
        self.assertEqual(snapshots[0], snapshots[1])

    def test_collision_replay_is_idempotent_and_survives_reopen(self):
        messages = (mail(), mail("LIFE-2", "COUPON-2", message_id="other"))
        segments = self.sync(*messages)
        before = self.fixture.count("canonical_segment_revisions")
        self.sync(*messages)
        self.assertEqual(self.fixture.count("canonical_segment_revisions"), before)
        self.fixture.repo.close()
        self.fixture.repo = BookingRepository(self.fixture.path, as_of=fixtures.NOW)
        self.assertEqual(self.fixture.repo.current_segments(), segments)
        self.assert_collision(segments)

    def test_unknown_lifetime_is_not_assigned_or_rekeyed(self):
        original = self.sync(fixtures.mail(sequence=None))[0]
        self.assertEqual(self.booking().lifetime_compatibility.status, LifetimeStatus.UNKNOWN)
        segments = self.sync(mail(message_id="known"))
        compatibility = self.booking().lifetime_compatibility
        self.assertEqual(compatibility.status, LifetimeStatus.UNKNOWN)
        self.assertEqual(compatibility.lifetimes, ("LIFE-1",))
        self.assertTrue(compatibility.has_unattributed_evidence)
        self.assertEqual(segments[0].segment_id, original.segment_id)

    def test_reinstatement_preserved_for_compatible_booking(self):
        self.sync(mail(kind="cancellation", sequence=3))
        segments = self.sync(reinstated(message_id="reinstate"))
        self.assertEqual(segments[0].status, "BOOKED")
        self.assertEqual(self.booking().lifetime_compatibility.status, LifetimeStatus.COMPATIBLE)
        segments = self.sync(mail("LIFE-2", "COUPON-2", message_id="unrelated-lifetime"))
        self.assert_collision(segments)

    def test_other_pnr_does_not_block_compatible_booking(self):
        other = mail("LIFE-2", message_id="other-booking")
        other = replace(other, text_body=other.text_body.replace("ABC123", "XYZ789"))
        segments = self.sync(mail(), other)
        self.assertEqual(len(self.fixture.repo.bookings()), 2)
        self.assertTrue(all(s.status == "BOOKED" for s in segments))

    def test_constraint_does_not_change_compatible_projection(self):
        event = booking_event(ItineraryExtractor().extract(mail()))
        segment = project("s", "b", (event,))
        self.assertEqual(constrain_booking_lifetime(segment, booking_lifetime_compatibility((event,))), segment)

    def test_read_boundary_blocks_pre_correction_persisted_active_state(self):
        # Reproduce a checkpoint written by the prior projector without rewriting
        # immutable history. Opening/reading it must not expose active collisions.
        with patch("travel_agent.live.booking_repository.constrain_booking_lifetime", side_effect=lambda s, c: s):
            self.sync(mail(), mail("LIFE-2", "COUPON-2", message_id="other"))
        self.assert_collision(self.fixture.repo.current_segments())
