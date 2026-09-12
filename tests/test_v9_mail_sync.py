"""Synthetic Gmail/Outlook-style ports only. No API clients or personal mail."""
from dataclasses import replace
from datetime import timedelta
from itertools import permutations
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_v9_mail_extraction import NOW, message
from travel_agent.live.booking import EventAuthority, Order, planning_readiness, project
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.extraction import ItineraryExtractor
from travel_agent.live.mail_sync import MailSynchronization
from travel_agent.live.providers import MailSyncPage, ProviderError, ProviderErrorCode, ProviderResult
from travel_agent.live.time import parse_instant


TRAVELERS = frozenset({"TRAVELER-1"})


def mail(kind="booking", sequence=1, *, provider="GMAIL", account_id="account-1", **updates):
    original = message(kind, provider=provider, account_id=account_id)
    body = original.text_body
    if sequence is not None:
        body += f"\nBooking lifetime: LIFE-1\nAirline sequence: {sequence}"
    return replace(original, text_body=body, **updates)


def page(messages=(), cursor="cursor-1", *, next_token=None, removed=()):
    return ProviderResult(value=MailSyncPage(tuple(messages), tuple(removed), next_token, None if next_token else cursor))


class OfflineMailSource:
    """Scripted normalized port used for both mailbox styles; tokens stay opaque."""
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.calls = []

    def sync(self, **arguments):
        self.calls.append(arguments)
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result


class GmailStyleSource(OfflineMailSource):
    pass


class OutlookStyleSource(OfflineMailSource):
    pass


class MailSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "booking.sqlite3"
        self.repo = BookingRepository(self.path, as_of=NOW)
        self.addCleanup(lambda: self.repo.close())
        self.service = MailSynchronization(self.repo, authorized_travelers=TRAVELERS)
        self.tick = 0
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Network forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def sync(self, *messages, provider="GMAIL", account_id="account-1", pages=None, full=False, service=None):
        self.tick += 1
        source_type = GmailStyleSource if provider == "GMAIL" else OutlookStyleSource
        source = source_type(*(pages if pages is not None else (page(messages, f"cursor-{self.tick}"),)))
        result = (service or self.service).run(source, provider, account_id, as_of=NOW + timedelta(minutes=self.tick), full=full)
        return result, source

    def count(self, table):
        return self.repo.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def current(self):
        return self.repo.current_segments()[0]

    def checkpoint(self):
        return (self.repo.sync_state("GMAIL", "account-1")["cursor"], self.repo.current_segments(),
                self.count("mail_messages"), self.count("booking_events"), self.count("canonical_segment_revisions"))

    def test_initial_booking_and_aware_handoff(self):
        result, source = self.sync(mail())
        self.assertTrue(result.success)
        self.assertIsNone(source.calls[0]["cursor"])
        self.assertEqual(len(self.repo.bookings()), 1)
        self.assertEqual(len(self.repo.current_segments()), 1)
        current = self.current()
        readiness = planning_readiness(current, authorized_travelers=TRAVELERS)
        self.assertTrue(readiness.planning_allowed)
        self.assertEqual(readiness.ready.segment_id, current.segment_id)
        self.assertEqual(current.schedule.scheduled_departure, parse_instant("2026-09-12T13:00Z"))
        self.assertIsNotNone(current.schedule.scheduled_arrival.tzinfo)
        self.assertEqual(current.schedule.origin_timezone, "America/New_York")
        self.assertEqual(self.count("segments"), 0)

    def test_exact_replay_and_cross_provider_equivalence(self):
        self.sync(mail(), mail())
        original = self.current()
        self.sync(mail())
        self.sync(mail(provider="OUTLOOK_MAIL", account_id="outlook"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.assertEqual(self.current(), original)
        self.assertEqual(self.count("canonical_segment_revisions"), 1)
        self.assertEqual(self.count("booking_events"), 1)
        evidence = self.repo.evidence(original.segment_id)
        self.assertEqual({r["provider"] for r in evidence}, {"GMAIL", "OUTLOOK_MAIL"})
        self.assertEqual(len(evidence), 2)

    def test_change_cancel_and_old_replay_preserve_history(self):
        self.sync(mail())
        original = self.current()
        self.sync(mail("change", 2))
        changed = self.current()
        self.assertEqual(original.segment_id, changed.segment_id)
        self.assertNotEqual(original.schedule.scheduled_departure, changed.schedule.scheduled_departure)
        self.assertEqual(len(self.repo.history(original.segment_id)), 2)
        self.sync(mail("cancellation", 3))
        cancelled = self.current()
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertFalse(planning_readiness(cancelled, authorized_travelers=TRAVELERS).planning_allowed)
        self.sync(mail(), mail("change", 2, message_id="late-copy"))
        self.assertEqual(self.current(), cancelled)
        self.assertEqual(len(self.repo.history(original.segment_id)), 3)
        self.assertTrue(any('13:00:00+00:00' in r["projection_json"] for r in self.repo.history(original.segment_id)))

    def test_older_change_does_not_overwrite_newer_change(self):
        self.sync(mail("change", 5))
        current = self.current()
        older = mail("booking", 2, message_id="older-change")
        older = replace(older, subject=mail("change").subject, text_body=older.text_body.replace("Event: BOOKING", "Event: CHANGE"))
        self.sync(older)
        self.assertEqual(self.current(), current)
        self.assertEqual(self.count("canonical_segment_revisions"), 1)

    def test_newer_sequence_alone_cannot_supersede_older_cancellation(self):
        self.sync(mail("change", 5), mail("cancellation", 3))
        self.assertEqual(self.current().status, "CANCELLED")
        self.sync(mail("cancellation", 3, message_id="late-cancel"))
        self.assertEqual(self.current().status, "CANCELLED")

    def test_cross_provider_change_and_cancellation(self):
        self.sync(mail())
        identity = self.current().segment_id
        self.sync(mail("change", 2, provider="OUTLOOK_MAIL", account_id="outlook"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.assertEqual(self.current().segment_id, identity)
        self.assertEqual(self.current().schedule.scheduled_departure.hour, 14)
        self.sync(mail("cancellation", 3, provider="OUTLOOK_MAIL", account_id="outlook"), provider="OUTLOOK_MAIL", account_id="outlook")
        self.assertEqual(self.current().status, "CANCELLED")
        self.assertEqual(self.current().segment_id, identity)

    def test_distinct_segments_and_bookings_do_not_collapse(self):
        first = mail()
        second = replace(first, message_id="return", text_body=first.text_body.replace("COUPON-1", "COUPON-2"))
        third = replace(first, message_id="other-pnr", text_body=first.text_body.replace("ABC123", "XYZ789"))
        self.sync(first, second, third)
        self.assertEqual(len(self.repo.bookings()), 2)
        self.assertEqual(len(self.repo.current_segments()), 3)

    def test_removal_and_full_absence_only_change_mailbox_visibility(self):
        self.sync(mail())
        before = self.current()
        self.sync(pages=(page(removed=("booking", "never-seen")),))
        self.assertEqual(self.current(), before)
        self.assertEqual(self.count("mail_removals"), 2)
        self.sync(mail(), full=True)
        self.sync(full=True)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.count("mail_messages"), 1)
        reasons = {r[0] for r in self.repo.connection.execute("SELECT reason FROM mail_removals")}
        self.assertEqual(reasons, {"PROVIDER_REMOVED", "ABSENT_FROM_FULL_SYNC"})
        self.assertEqual(self.repo.connection.execute("SELECT visible FROM mailbox_visibility WHERE message_id='booking'").fetchone()[0], 0)

    def test_full_sync_absence_is_scoped_to_lookback(self):
        old = mail(received_at=NOW - timedelta(days=500))
        self.sync(old)
        self.sync(full=True)
        self.assertEqual(self.count("mail_removals"), 0)

    def test_multiple_pages_empty_page_and_completed_empty_delta(self):
        result, source = self.sync(pages=(page(next_token="a"), page((mail(),), next_token="b"), page(cursor="end")))
        self.assertTrue(result.success)
        self.assertEqual([c["page_token"] for c in source.calls], [None, "a", "b"])
        before = self.current()
        result, source = self.sync()
        self.assertTrue(result.success)
        self.assertEqual(source.calls[0]["cursor"], "end")
        self.assertEqual(self.current(), before)

    def test_partial_page_failure_rolls_back_and_records_safe_error(self):
        self.sync(mail())
        before = self.checkpoint()
        failure = ProviderResult(error=ProviderError(ProviderErrorCode.UNAVAILABLE, True))
        result, _ = self.sync(pages=(page((mail("change", 2),), next_token="a"), failure))
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1")["status"], "ERROR")

    def test_cursor_expiration_requires_explicit_full_resync(self):
        self.sync(mail())
        before = self.current()
        expired = ProviderResult(error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False))
        result, _ = self.sync(pages=(expired,))
        self.assertTrue(result.resync_required)
        result, source = self.sync()
        self.assertTrue(result.resync_required)
        self.assertEqual(source.calls, [])
        result, source = self.sync(full=True)
        self.assertTrue(result.success)
        self.assertIsNone(source.calls[0]["cursor"])
        self.assertEqual(self.current(), before)

    def test_failed_full_resync_does_not_clear_expiration_requirement(self):
        self.sync(mail())
        self.sync(pages=(ProviderResult(error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False)),))
        result, _ = self.sync(full=True, pages=(ProviderResult(error=ProviderError(ProviderErrorCode.UNAVAILABLE, True)),))
        self.assertFalse(result.success)
        self.assertTrue(result.resync_required)
        result, source = self.sync()
        self.assertTrue(result.resync_required)
        self.assertEqual(source.calls, [])
        self.assertTrue(self.sync(full=True)[0].success)
        self.assertFalse(self.repo.resync_required("GMAIL", "account-1"))

    def test_extraction_failure_rolls_back(self):
        self.sync(mail())
        before = self.checkpoint()
        class Failure(ItineraryExtractor):
            def extract(self, message):
                raise RuntimeError("private payload must not be recorded")
        service = MailSynchronization(self.repo, authorized_travelers=TRAVELERS, extractor=Failure())
        result, _ = self.sync(mail("change", 2), service=service)
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)
        self.assertNotIn("private", self.repo.sync_state("GMAIL", "account-1")["error_json"])

    def test_projection_failure_rolls_back_inserted_evidence(self):
        self.sync(mail())
        before = self.checkpoint()
        def failure(*args):
            self.assertEqual(self.count("booking_events"), 2)
            raise RuntimeError("Injected failure")
        service = MailSynchronization(self.repo, authorized_travelers=TRAVELERS, projector=failure)
        result, _ = self.sync(mail("change", 2), service=service)
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)

    def test_reconciliation_failure_rolls_back(self):
        self.sync(mail())
        before = self.checkpoint()
        with patch("travel_agent.live.booking_repository.reconcile", side_effect=RuntimeError("failure")):
            result, _ = self.sync(mail("change", 2))
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)

    def test_first_sync_failure_leaves_no_partial_canonical_rows(self):
        def failure(*args):
            raise RuntimeError("failure")
        service = MailSynchronization(self.repo, authorized_travelers=TRAVELERS, projector=failure)
        result, _ = self.sync(mail(), service=service)
        self.assertFalse(result.success)
        self.assertEqual(self.repo.current_segments(), ())
        self.assertEqual(self.repo.bookings(), ())
        self.assertEqual(self.count("mail_messages"), 0)
        self.assertIsNone(self.repo.sync_state("GMAIL", "account-1")["cursor"])

    def test_cancellation_arriving_first_is_not_resurrected(self):
        self.sync(mail("cancellation", 3))
        before = self.current()
        self.sync(mail("change", 2))
        self.sync(mail())
        self.assertEqual(self.current(), before)
        self.assertEqual(self.count("canonical_segment_revisions"), 1)
        self.assertEqual(self.count("booking_events"), 3)

    def test_newer_normal_change_preserves_cancellation(self):
        self.sync(mail("cancellation", 3))
        self.sync(mail("change", 4))
        self.assertEqual(self.current().status, "CANCELLED")
        self.assertEqual(self.current().current_authority.sequence, 3)
        self.assertEqual(self.count("canonical_segment_revisions"), 1)

    def test_realistic_return_route_preserves_two_segments_same_pnr(self):
        outbound = mail()
        body = outbound.text_body.replace("COUPON-1", "RETURN-1").replace("NS123", "NS456")
        body = body.replace("Origin: ATL", "Origin: LAX").replace("Destination: LAX", "Destination: ATL")
        body = body.replace("Origin timezone: America/New_York", "Origin timezone: America/Los_Angeles")
        body = body.replace("Destination timezone: America/Los_Angeles", "Destination timezone: America/New_York")
        body = body.replace("2026-09-12", "2026-09-20").replace("09:00-04:00", "09:00-07:00").replace("11:00-07:00", "17:00-04:00")
        self.sync(outbound, replace(outbound, message_id="return", text_body=body))
        self.assertEqual(len(self.repo.bookings()), 1)
        self.assertEqual(len(self.repo.current_segments()), 2)
        self.assertEqual({s.schedule.origin for s in self.repo.current_segments()}, {"ATL", "LAX"})
        self.assertTrue(all(planning_readiness(s, authorized_travelers=TRAVELERS).planning_allowed for s in self.repo.current_segments()))

    def test_opaque_message_version_adds_evidence_not_revision(self):
        self.sync(mail())
        self.sync(mail(version="opaque-new-change-key"))
        self.assertEqual(self.count("mail_messages"), 2)
        self.assertEqual(self.count("booking_events"), 1)
        self.assertEqual(self.count("canonical_segment_revisions"), 1)

    def test_sync_checkpoint_history_retains_projection_versions(self):
        self.sync(mail())
        self.sync(mail("change", 2))
        self.sync(mail("cancellation", 3))
        self.assertEqual(self.count("sync_projections"), 3)
        revisions = {r[0] for r in self.repo.connection.execute("SELECT revision_id FROM sync_projections")}
        self.assertEqual(len(revisions), 3)

    def test_booking_repository_refuses_phase3_cursor_only_bypass(self):
        with self.assertRaisesRegex(ValueError, "complete MailSynchronization"):
            self.repo.complete_sync()
        with self.assertRaisesRegex(ValueError, "complete MailSynchronization"):
            self.repo.store_messages()

    def test_readiness_rejects_naive_time_and_invalid_zone(self):
        self.sync(mail())
        current = self.current()
        for schedule in (replace(current.schedule, scheduled_departure=current.schedule.scheduled_departure.replace(tzinfo=None)),
                         replace(current.schedule, origin_timezone="Unknown/Zone")):
            result = planning_readiness(replace(current, schedule=schedule), authorized_travelers=TRAVELERS)
            self.assertFalse(result.planning_allowed)
            self.assertIn("INVALID_DEPARTURE_TIME", result.reasons)

    def test_failure_after_projection_before_cursor_rolls_everything_back(self):
        self.sync(mail())
        before = self.checkpoint()
        self.repo.connection.set_authorizer(lambda action, table, *args:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "mail_sync_runs" else sqlite3.SQLITE_OK)
        try:
            result, _ = self.sync(mail("change", 2))
        finally:
            self.repo.connection.set_authorizer(None)
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)

    def test_ambiguous_order_never_uses_receipt_time_or_message_version(self):
        self.sync(mail(sequence=None))
        change = mail("change", None, version="999", received_at=NOW + timedelta(minutes=1))
        self.sync(change)
        self.assertEqual(self.current().status, "UNRESOLVED")
        self.assertIsNone(self.current().schedule)
        self.assertEqual(self.count("booking_events"), 2)
        self.assertFalse(planning_readiness(self.current(), authorized_travelers=TRAVELERS).planning_allowed)

    def test_equal_sequence_conflict_cannot_be_hidden_by_later_sequence(self):
        self.sync(mail(), mail("change", 1), mail("cancellation", 3))
        self.assertEqual(self.current().status, "UNRESOLVED")

    def test_reused_lifetime_reference_remains_unresolved(self):
        second = mail("change", 20)
        second = replace(second, text_body=second.text_body.replace("LIFE-1", "LIFE-2"))
        self.sync(mail(), second)
        self.assertEqual(self.current().status, "UNRESOLVED")

    def test_invalid_or_multipart_conflicting_authority_fails_closed(self):
        for suffix in ("\nAirline sequence: 2", "\nBooking lifetime: LIFE-2"):
            original = mail()
            malformed = replace(original, message_id=suffix, html_body=f"<p>{suffix.strip()}</p>")
            self.sync(malformed)
        self.assertEqual(self.current().status, "UNRESOLVED")

    def test_missing_planning_fields_and_traveler_authority(self):
        original = mail()
        body = "\n".join(line for line in original.text_body.splitlines()
            if not line.startswith(("Departure:", "Origin timezone:", "Arrival:", "Destination timezone:")))
        self.sync(replace(original, text_body=body))
        self.assertEqual(self.current().status, "BOOKED")
        readiness = planning_readiness(self.current(), authorized_travelers=frozenset())
        self.assertFalse(readiness.planning_allowed)
        self.assertIn("MISSING_DEPARTURE", readiness.reasons)
        self.assertIn("MISSING_ORIGIN_TIMEZONE", readiness.reasons)
        self.assertIn("TRAVELER_NOT_AUTHORIZED", readiness.reasons)

    def test_unknown_identity_and_nontravel_are_preserved_without_projection(self):
        unknown = mail()
        unknown = replace(unknown, text_body=unknown.text_body.replace("TRAVELER-1", "UNKNOWN"))
        self.sync(unknown, message("incomplete"), message("unrelated"))
        self.assertEqual(self.repo.current_segments(), ())
        self.assertEqual(len(self.repo.unresolved_identity(authorized_travelers=TRAVELERS)), 2)
        self.assertEqual(self.count("mail_messages"), 3)

    def test_message_collision_rolls_back_checkpoint(self):
        self.sync(mail())
        before = self.checkpoint()
        collision = mail()
        result, _ = self.sync(replace(collision, text_body=collision.text_body.replace("NS123", "NS999")))
        self.assertFalse(result.success)
        self.assertEqual(self.checkpoint(), before)

    def test_page_cycle_and_ambiguous_visibility_are_rejected(self):
        self.sync(mail())
        before = self.checkpoint()
        for pages in ((page(next_token="a"), page(next_token="a")),
                      (page((mail("change", 2),), removed=("change",)),),
                      (ProviderResult(value=MailSyncPage((), (), None, None)),)):
            result, _ = self.sync(pages=pages)
            self.assertFalse(result.success)
            self.assertEqual(self.checkpoint(), before)

    def test_scope_future_evidence_and_limits_rejected(self):
        self.sync(mail())
        before = self.checkpoint()
        for incoming in (mail(provider="OUTLOOK_MAIL"), mail(received_at=NOW + timedelta(days=1))):
            result, _ = self.sync(incoming)
            self.assertFalse(result.success)
            self.assertEqual(self.checkpoint(), before)
        bounded = MailSynchronization(self.repo, authorized_travelers=TRAVELERS, max_pages=1, max_messages=1)
        result, _ = self.sync(pages=(page(next_token="a"),), service=bounded)
        self.assertFalse(result.success)
        result, _ = self.sync(mail(), mail(), service=bounded)
        self.assertFalse(result.success)

    def test_reopen_preserves_projection_provenance_and_cursor(self):
        self.sync(mail(), mail("change", 2))
        before = self.checkpoint()
        self.repo.close()
        self.repo = BookingRepository(self.path, as_of=NOW)
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(len(self.repo.evidence(self.current().segment_id)), 2)

    def test_all_event_permutations_produce_same_current_state(self):
        from travel_agent.live.booking import booking_event
        extractor = ItineraryExtractor()
        events = tuple(booking_event(extractor.extract(m)) for m in (mail(), mail("change", 2), mail("cancellation", 3)))
        results = [project("s", "b", order) for order in permutations(events)]
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0].status, "CANCELLED")

    def test_typed_order_contract(self):
        first = EventAuthority("LIFE-1", 1)
        self.assertEqual(first.compare(EventAuthority("LIFE-1", 2)), Order.OLDER)
        self.assertEqual(first.compare(first), Order.EQUAL)
        self.assertEqual(first.compare(EventAuthority("LIFE-2", 1)), Order.UNKNOWN)
        self.assertEqual(EventAuthority("LIFE-1", 2).compare(first), Order.NEWER)
        for value in (True, -1, "2", 2147483648):
            with self.assertRaises(ValueError):
                EventAuthority("LIFE-1", value)

    def test_concurrent_checkpoint_is_not_overwritten(self):
        self.sync(mail())
        repo = self.repo
        class ConcurrentSource:
            def sync(self, **kwargs):
                repo.sync_failed("GMAIL", "account-1", error=ProviderError(ProviderErrorCode.UNAVAILABLE, True), as_of=NOW + timedelta(minutes=2))
                return page((mail("change", 2),))
        before = self.current()
        result = self.service.run(ConcurrentSource(), "GMAIL", "account-1", as_of=NOW + timedelta(minutes=3))
        self.assertFalse(result.success)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.count("mail_messages"), 1)
