"""Both failure APIs preserve full-resync requirements; every source is offline."""
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import patch

import test_v9_mail_sync as fixtures
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.repository import LiveRepository
from travel_agent.live.providers import (ProviderError, ProviderErrorCode, ProviderResult,
    FlightIdentity, FlightObservation, TrafficRequest, TrafficEstimate)
from travel_agent.live.observations import Provenance, RetrievedBy


class ResyncRequirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailSyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.assertTrue(self.fixture.sync(fixtures.mail())[0].success)

    @property
    def repo(self):
        return self.fixture.repo

    def required(self):
        return self.repo.resync_required("GMAIL", "account-1")

    def fail(self, code=ProviderErrorCode.CURSOR_EXPIRED, *, conditional=False):
        self.fixture.tick += 1
        args = dict(error=ProviderError(code, code != ProviderErrorCode.CURSOR_EXPIRED),
                    as_of=fixtures.NOW + timedelta(minutes=self.fixture.tick))
        if conditional:
            self.repo.record_sync_failure("GMAIL", "account-1",
                expected_state=self.repo.sync_state("GMAIL", "account-1"), **args)
        else:
            self.repo.sync_failed("GMAIL", "account-1", **args)

    def test_cursor_expired_sets_durable_flag_without_changing_success(self):
        before = self.fixture.checkpoint()
        successful_at = self.repo.sync_state("GMAIL", "account-1")["last_success_at"]
        self.fail()
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1")["last_success_at"], successful_at)
        self.assertEqual(self.repo.connection.execute("SELECT resync_required FROM provider_resync_requirements").fetchone()[0], 1)

    def test_legacy_signature_unavailable_cannot_clear_expiration(self):
        self.fail()
        self.fail(ProviderErrorCode.UNAVAILABLE)
        self.assertTrue(self.required())

    def test_transient_auth_and_cursor_failures_remain_required(self):
        self.fail()
        for code in (ProviderErrorCode.UNAVAILABLE, ProviderErrorCode.AUTH_REQUIRED,
                     ProviderErrorCode.PERMISSION_DENIED, ProviderErrorCode.RATE_LIMITED,
                     ProviderErrorCode.INVALID_RESPONSE, ProviderErrorCode.CURSOR_EXPIRED):
            self.fail(code)
            self.assertTrue(self.required())

    def test_timeout_during_full_attempt_keeps_requirement(self):
        self.fail()
        before = self.fixture.checkpoint()
        outcome, _ = self.fixture.sync(full=True, pages=(TimeoutError("synthetic timeout"),))
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.resync_required)
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)

    def test_failed_full_pagination_preserves_requirement(self):
        self.fail()
        before = self.fixture.checkpoint()
        outcome, _ = self.fixture.sync(full=True, pages=(
            fixtures.page((fixtures.mail("change", 2),), next_token="next"),
            ProviderResult(error=ProviderError(ProviderErrorCode.UNAVAILABLE, True))))
        self.assertFalse(outcome.success)
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)

    def test_successful_provider_delta_is_not_accepted_as_recovery(self):
        self.fail()
        before = self.fixture.checkpoint()
        outcome, source = self.fixture.sync(pages=(fixtures.page(cursor="successful-delta"),))
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.resync_required)
        self.assertEqual(source.calls, [])
        self.assertEqual(self.fixture.checkpoint(), before)
        # The repository also enforces this if the application guard is bypassed.
        with self.assertRaisesRegex(ValueError, "Explicit full resync"):
            self.repo.commit_sync("GMAIL", "account-1",
                expected_state=self.repo.sync_state("GMAIL", "account-1"),
                completed_cursor="successful-delta", messages=(), removed_ids=frozenset(),
                full=False, since=fixtures.NOW - timedelta(days=365),
                as_of=fixtures.NOW + timedelta(minutes=10), authorized_travelers=fixtures.TRAVELERS)
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)

    def test_qualifying_full_sync_clears_flag_and_incremental_resumes(self):
        self.fail()
        outcome, source = self.fixture.sync(full=True, pages=(fixtures.page(next_token="next"), fixtures.page(cursor="recovered")))
        self.assertTrue(outcome.success)
        self.assertTrue(all(call["cursor"] is None for call in source.calls))
        self.assertFalse(self.required())
        outcome, source = self.fixture.sync()
        self.assertTrue(outcome.success)
        self.assertEqual(source.calls[0]["cursor"], "recovered")
        self.assertFalse(self.required())

    def test_restart_preserves_requirement_after_unrelated_error(self):
        self.fail()
        self.fail(ProviderErrorCode.UNAVAILABLE)
        self.repo.close()
        self.fixture.repo = BookingRepository(self.fixture.path, as_of=fixtures.NOW)
        self.fixture.service = fixtures.MailSynchronization(self.repo, authorized_travelers=fixtures.TRAVELERS)
        self.assertTrue(self.required())
        self.assertFalse(self.fixture.sync()[0].success)

    def test_repeated_expiration_is_idempotently_required(self):
        self.fail()
        args = dict(error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False),
                    as_of=fixtures.NOW + timedelta(minutes=self.fixture.tick))
        before = list(self.repo.connection.iterdump())
        self.repo.sync_failed("GMAIL", "account-1", **args)
        self.assertEqual(list(self.repo.connection.iterdump()), before)
        self.assertTrue(self.required())

    def test_application_and_legacy_failure_paths_share_semantics(self):
        outcome, _ = self.fixture.sync(pages=(ProviderResult(error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False)),))
        self.assertTrue(outcome.resync_required)
        self.fail(ProviderErrorCode.UNAVAILABLE)
        self.fail(ProviderErrorCode.AUTH_REQUIRED, conditional=True)
        self.assertTrue(self.required())

    def test_normal_success_and_failure_without_expiration_unchanged(self):
        self.fail(ProviderErrorCode.UNAVAILABLE)
        self.assertFalse(self.required())
        outcome, source = self.fixture.sync()
        self.assertTrue(outcome.success)
        self.assertIsNotNone(source.calls[0]["cursor"])
        self.assertFalse(self.required())

    def test_extraction_reconciliation_and_projection_failures_do_not_clear(self):
        self.fail()
        before = self.fixture.checkpoint()
        for target in ("travel_agent.live.extraction.ItineraryExtractor.extract",
                       "travel_agent.live.booking_repository.reconcile"):
            with patch(target, side_effect=RuntimeError("synthetic failure")):
                outcome, _ = self.fixture.sync(fixtures.mail("change", 2), full=True)
            self.assertFalse(outcome.success)
            self.assertTrue(self.required())
            self.assertEqual(self.fixture.checkpoint(), before)
        def failure(*args):
            raise RuntimeError("synthetic projection failure")
        service = fixtures.MailSynchronization(self.repo, authorized_travelers=fixtures.TRAVELERS, projector=failure)
        outcome, _ = self.fixture.sync(fixtures.mail("change", 2), full=True, service=service)
        self.assertFalse(outcome.success)
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)

    def test_database_failure_rolls_back_full_recovery_and_flag(self):
        self.fail()
        before = self.fixture.checkpoint()
        # Fail the last flag write, after projection and cursor updates. The entire
        # checkpoint must roll back; failed audit writes must not clear the latch.
        self.repo.connection.set_authorizer(lambda action, table, *args:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "provider_resync_requirements" else sqlite3.SQLITE_OK)
        try:
            outcome, _ = self.fixture.sync(fixtures.mail("change", 2), full=True)
        finally:
            self.repo.connection.set_authorizer(None)
        self.assertFalse(outcome.success)
        self.assertTrue(self.required())
        self.assertEqual(self.fixture.checkpoint(), before)

    def test_phase3_flight_traffic_and_retrieval_apis_remain_available(self):
        self.fail()
        segment = self.repo.current_segments()[0]
        provenance = Provenance("offline-fixture", fixtures.NOW, RetrievedBy.SIMULATED)
        flight = FlightObservation(FlightIdentity(segment.segment_id, "NS123", "ATL", "LAX",
            segment.schedule.scheduled_departure, "America/New_York"), segment.schedule.scheduled_departure,
            False, None, None, provenance)
        traffic = TrafficEstimate(TrafficRequest("HOME", "ATL", fixtures.NOW), 40, provenance)
        for identifier, kind, observation in (("flight", "FLIGHT", flight), ("traffic", "TRAFFIC", traffic)):
            self.repo.store_observation(identifier, observation, segment_id=segment.segment_id, retrieved_at=fixtures.NOW)
            self.repo.record_retrieval(identifier, "FAKE", "account", kind, segment_id=segment.segment_id,
                as_of=fixtures.NOW, observation_id=identifier)
        self.assertEqual(len(self.repo.observations(segment.segment_id)), 2)
        self.assertEqual(self.fixture.count("retrieval_attempts"), 2)
        self.assertTrue(self.required())

    def test_upgrade_legacy_expired_error_is_latched_before_overwrite(self):
        path = self.fixture.path.parent / "phase3.sqlite3"
        legacy = LiveRepository(path, as_of=fixtures.NOW)
        legacy.sync_failed("GMAIL", "account", error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False), as_of=fixtures.NOW)
        legacy.close()
        upgraded = BookingRepository(path, as_of=fixtures.NOW)
        try:
            upgraded.sync_failed("GMAIL", "account", error=ProviderError(ProviderErrorCode.UNAVAILABLE, True),
                as_of=fixtures.NOW + timedelta(minutes=1))
            self.assertTrue(upgraded.resync_required("GMAIL", "account"))
        finally:
            upgraded.close()

    def test_stale_conditional_failure_cannot_undo_successful_full_recovery(self):
        self.fail()
        old = self.repo.sync_state("GMAIL", "account-1")
        self.assertTrue(self.fixture.sync(full=True)[0].success)
        self.repo.record_sync_failure("GMAIL", "account-1", expected_state=old,
            error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False), as_of=fixtures.NOW + timedelta(minutes=10))
        self.assertFalse(self.required())

    def test_legacy_signature_preserves_out_of_order_rejection(self):
        self.fail()
        with self.assertRaisesRegex(ValueError, "Out-of-order"):
            self.repo.sync_failed("GMAIL", "account-1", error=ProviderError(ProviderErrorCode.UNAVAILABLE, True), as_of=fixtures.NOW)
        self.assertTrue(self.required())

    def test_other_account_success_cannot_clear_required_account(self):
        self.fail()
        outcome, _ = self.fixture.sync(provider="OUTLOOK_MAIL", account_id="other", full=True)
        self.assertTrue(outcome.success)
        self.assertTrue(self.required())
        self.assertFalse(self.repo.resync_required("OUTLOOK_MAIL", "other"))
