from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from test_itinerary_agent import Source
from test_itinerary_service import ServiceFixture
from test_v9_mail_extraction import NOW, message
from travel_agent.agents.itinerary_agent import ItineraryAgent
from travel_agent.itinerary.repository import ItineraryRepository
from travel_agent.live.migrations import migrate, schema_signature, expected_schema, MIGRATION_CHECKSUM
from travel_agent.live.observations import (Provenance, RetrievedBy, SecurityObservation,
    LocationObservation, ParkingObservation, RideshareObservation, ParkingCategory, Availability, freshness, DataStatus)
from travel_agent.live.providers import (ProviderError, ProviderErrorCode, FlightIdentity, FlightObservation,
    TrafficRequest, TrafficEstimate)
from travel_agent.live.reconciliation import reconcile
from travel_agent.live.repository import LiveRepository


LEGACY_TABLES = ("refresh_runs", "itineraries", "itinerary_revisions", "segments", "automatic_planning_state", "planning_attempts")


def legacy_rows(connection):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in LEGACY_TABLES}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"

    def legacy(self):
        repo = ItineraryRepository(self.path)
        source = Source()
        repo.apply_snapshot(ItineraryAgent(source).refresh(), as_of=datetime(2026, 9, 10, 19))
        source.data["records"][0]["segments"][0]["scheduled_departure"] = "2026-09-11T20:00"
        repo.apply_snapshot(ItineraryAgent(source).refresh(), as_of=datetime(2026, 9, 10, 20))
        return repo

    def test_new_database_current_schema_and_idempotency(self):
        repo = LiveRepository(self.path, as_of=NOW)
        self.addCleanup(repo.close)
        self.assertEqual(schema_signature(repo.connection), expected_schema(current=True))
        before = list(repo.connection.iterdump())
        migrate(repo.connection, as_of=NOW + timedelta(days=1))
        self.assertEqual(list(repo.connection.iterdump()), before)
        self.assertEqual(tuple(repo.connection.execute("SELECT version,checksum FROM schema_migrations").fetchone()), (1, MIGRATION_CHECKSUM))
        self.assertEqual(repo.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_v8_history_backup_and_triggers_preserved(self):
        old = self.legacy()
        before = legacy_rows(old.connection)
        self.assertEqual(len(before["itinerary_revisions"]), 2)
        old.close()
        backup_path = Path(self.temp.name) / "backup.sqlite3"
        live = LiveRepository(self.path, as_of=NOW, backup_path=backup_path)
        self.addCleanup(live.close)
        self.assertEqual(legacy_rows(live.connection), before)
        backup = sqlite3.connect(backup_path)
        self.addCleanup(backup.close)
        self.assertEqual(schema_signature(backup), expected_schema(current=False))
        self.assertEqual(legacy_rows(backup), before)
        for sql in ("UPDATE itinerary_revisions SET normalized_snapshot_json='{}'", "DELETE FROM itinerary_revisions"):
            with self.assertRaises(sqlite3.IntegrityError), live.connection:
                live.connection.execute(sql)
        self.assertEqual(legacy_rows(live.connection), before)
        # Unmodified V8 repository can still open and refresh a migrated database.
        reopened = ItineraryRepository(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.list_current_segments()), 1)

    def test_partial_unknown_or_altered_schema_refused(self):
        for sql in ("CREATE TABLE segments(x TEXT)", "CREATE TABLE unrelated(x TEXT)"):
            connection = sqlite3.connect(":memory:")
            try:
                connection.execute(sql)
                before = list(connection.iterdump())
                with self.assertRaisesRegex(ValueError, "Unrecognized"):
                    migrate(connection, as_of=NOW)
                self.assertEqual(list(connection.iterdump()), before)
            finally:
                connection.close()
        old = self.legacy()
        old.connection.execute("DROP TRIGGER immutable_revision_update")
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            migrate(old.connection, as_of=NOW)
        old.close()

    def test_migration_failure_rolls_back_all_ddl_and_history(self):
        old = self.legacy()
        self.addCleanup(old.close)
        before = list(old.connection.iterdump())
        old.connection.set_authorizer(lambda action, arg1, arg2, db, trigger:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == "observations_by_segment_time" else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            migrate(old.connection, as_of=NOW)
        old.connection.set_authorizer(None)
        self.assertEqual(list(old.connection.iterdump()), before)
        migrate(old.connection, as_of=NOW)
        self.assertEqual(schema_signature(old.connection), expected_schema(current=True))

    def test_unknown_or_tampered_version_refused(self):
        repo = LiveRepository(self.path, as_of=NOW)
        self.addCleanup(repo.close)
        for sql in ("UPDATE schema_migrations SET checksum='tampered'", "UPDATE schema_migrations SET version=99"):
            with repo.connection:
                repo.connection.execute(sql)
            with self.assertRaisesRegex(ValueError, "migration history"):
                migrate(repo.connection, as_of=NOW)

    def test_foreign_key_corruption_rolls_back_migration(self):
        old = self.legacy()
        self.addCleanup(old.close)
        old.connection.execute("PRAGMA foreign_keys=OFF")
        with old.connection:
            old.connection.execute("UPDATE segments SET itinerary_id='missing'")
        before = list(old.connection.iterdump())
        with self.assertRaisesRegex(ValueError, "Foreign key"):
            migrate(old.connection, as_of=NOW)
        self.assertEqual(list(old.connection.iterdump()), before)

    def test_existing_backup_is_not_overwritten(self):
        old = self.legacy()
        self.addCleanup(old.close)
        backup = Path(self.temp.name) / "keep"
        backup.write_text("keep")
        with self.assertRaises(FileExistsError):
            migrate(old.connection, as_of=NOW, backup_path=backup)
        self.assertEqual(backup.read_text(), "keep")
        self.assertEqual(schema_signature(old.connection), expected_schema(current=False))

    def test_pending_transaction_rejected(self):
        old = self.legacy()
        self.addCleanup(old.close)
        old.connection.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "idle connection"):
            migrate(old.connection, as_of=NOW)
        old.connection.rollback()


class MigratedServiceTests(ServiceFixture):
    def test_completed_attempt_and_automatic_history_survive_migration(self):
        first = self.service.monitor_trips()["dispatches"][0]
        connection = self.service.repository.connection
        before = legacy_rows(connection)
        migrate(connection, as_of=NOW)
        self.assertEqual(legacy_rows(connection), before)
        second = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual(second["decision"], "SUPPRESS_DUPLICATE")
        self.assertEqual(second["attempt"], first["attempt"])


class LivePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.repo = LiveRepository(self.path, as_of=NOW)
        self.addCleanup(lambda: self.repo.close())
        self.error = ProviderError(ProviderErrorCode.UNAVAILABLE, True)

    def count(self, table):
        return self.repo.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def complete(self, **kwargs):
        args = dict(expected_cursor=None, completed_cursor="opaque-1", messages=(message(),), as_of=NOW)
        args.update(kwargs)
        return self.repo.complete_sync("GMAIL", "account-1", **args)

    def test_sync_roundtrip_failure_preserves_cursor_and_last_success(self):
        self.complete()
        self.repo.sync_failed("GMAIL", "account-1", error=self.error, as_of=NOW + timedelta(minutes=1))
        state = self.repo.sync_state("GMAIL", "account-1")
        self.assertEqual(state["cursor"], "opaque-1")
        self.assertEqual(state["last_success_at"], NOW.isoformat())
        self.assertEqual(state["status"], "ERROR")
        self.assertEqual(json.loads(state["error_json"])["code"], "UNAVAILABLE")
        self.repo.close()
        self.repo = LiveRepository(self.path, as_of=NOW)
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1"), state)
        self.complete(expected_cursor="opaque-1", completed_cursor="opaque-2", messages=(), as_of=NOW + timedelta(minutes=2))
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1")["status"], "SUCCESS")
        self.assertEqual(self.count("mail_messages"), 1)

    def test_failed_first_sync_does_not_invent_success(self):
        self.repo.sync_failed("OUTLOOK_MAIL", "a", error=self.error, as_of=NOW)
        state = self.repo.sync_state("OUTLOOK_MAIL", "a")
        self.assertIsNone(state["cursor"])
        self.assertIsNone(state["last_success_at"])

    def test_message_idempotency_and_reconciliation_after_reopen(self):
        other = message(provider="OUTLOOK_MAIL", account_id="other", message_id="different")
        self.repo.store_messages((message(), message(), other), as_of=NOW)
        self.assertEqual(self.count("mail_messages"), 2)
        self.repo.close()
        self.repo = LiveRepository(self.path, as_of=NOW)
        results = self.repo.extractions()
        result = reconcile(results, authorized_travelers=frozenset({"TRAVELER-1"}))
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(len(result.segments[0].evidence), 2)
        self.assertEqual(results[0].message, message())
        self.assertEqual(self.count("segments"), 0)

    def test_message_collision_rolls_back_batch_and_cursor(self):
        self.complete()
        collision = message(text_body=message().text_body.replace("NS123", "NS124"))
        with self.assertRaisesRegex(ValueError, "collision"):
            self.complete(expected_cursor="opaque-1", completed_cursor="opaque-2", messages=(message("change"), collision))
        self.assertEqual(self.count("mail_messages"), 1)
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1")["cursor"], "opaque-1")

    def test_failed_extraction_rolls_back_entire_first_sync(self):
        from travel_agent.live.extraction import ItineraryExtractor
        class FailingExtractor(ItineraryExtractor):
            def extract(self, mail):
                if mail.message_id == "change":
                    raise RuntimeError("Injected extraction failure")
                return super().extract(mail)
        with self.assertRaises(RuntimeError):
            self.complete(messages=(message(), message("change")), extractor=FailingExtractor())
        self.assertEqual(self.count("mail_messages"), 0)
        self.assertIsNone(self.repo.sync_state("GMAIL", "account-1"))

    def test_future_or_unattributed_mail_cannot_advance_cursor(self):
        for mail in (message(received_at=NOW + timedelta(seconds=1)), message(provenance=None)):
            with self.assertRaises(ValueError):
                self.complete(messages=(mail,))
        self.assertIsNone(self.repo.sync_state("GMAIL", "account-1"))

    def test_cursor_scope_and_time_guards(self):
        self.complete()
        for args in (dict(expected_cursor="wrong"), dict(expected_cursor="opaque-1", messages=(message(provider="OUTLOOK_MAIL"),)),
                     dict(expected_cursor="opaque-1", as_of=NOW - timedelta(seconds=1))):
            with self.assertRaises(ValueError):
                self.complete(**args)
        self.assertEqual(self.repo.sync_state("GMAIL", "account-1")["cursor"], "opaque-1")

    def test_unresolved_and_nontravel_evidence_never_create_v8_segments(self):
        self.repo.store_messages((message("incomplete"), message("conflicting"), message("unrelated")), as_of=NOW)
        self.assertEqual(self.count("mail_messages"), 3)
        self.assertEqual(self.count("segments"), 0)
        self.assertTrue(all(r.segment is None for r in self.repo.extractions()))

    def test_message_version_keeps_history(self):
        self.repo.store_messages((message(), message(version="v2")), as_of=NOW)
        self.assertEqual(self.count("mail_messages"), 2)
        with self.assertRaises(sqlite3.IntegrityError), self.repo.connection:
            self.repo.connection.execute("UPDATE mail_messages SET message_json='{}'")

    def security(self, segment="s1"):
        return SecurityObservation(segment, Provenance("airport", NOW, RetrievedBy.HOST), "ATL", "STANDARD", 10)

    def test_observation_roundtrip_failure_does_not_replace_good_data(self):
        observation = self.security()
        self.repo.store_observation("o1", observation, retrieved_at=NOW)
        self.repo.store_observation("o1", observation, retrieved_at=NOW)
        self.repo.record_retrieval("r1", "HOST", "a", "SECURITY", segment_id="s1", as_of=NOW, observation_id="o1")
        self.repo.record_retrieval("r2", "HOST", "a", "SECURITY", segment_id="s1", as_of=NOW, error=self.error)
        row = self.repo.observations("s1")[0]
        self.assertEqual(json.loads(row["payload_json"])["wait_minutes"], 10)
        self.assertEqual(row["observed_at"], NOW.isoformat())
        self.assertEqual(row["retrieved_by"], "HOST")
        self.assertEqual(self.count("live_observations"), 1)
        self.assertEqual(self.count("retrieval_attempts"), 2)
        self.assertEqual(freshness(observation, NOW, retrieval_failure_code="UNAVAILABLE").status, DataStatus.USABLE)

    def test_observation_collision_future_and_association_guards(self):
        observation = self.security()
        self.repo.store_observation("o1", observation, retrieved_at=NOW)
        with self.assertRaises(ValueError):
            self.repo.store_observation("o1", replace(observation, wait_minutes=20), retrieved_at=NOW)
        with self.assertRaises(ValueError):
            self.repo.store_observation("o2", observation, retrieved_at=NOW - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            self.repo.store_observation("o2", observation, retrieved_at=NOW, segment_id="wrong")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.store_observation("o2", observation, retrieved_at=NOW, legacy_segment_id="s1")
        with self.assertRaises(ValueError):
            self.repo.record_retrieval("r1", "HOST", "a", "SECURITY", segment_id="wrong", as_of=NOW, observation_id="o1")

    def test_optional_unscoped_location(self):
        location = LocationObservation(None, Provenance("host", NOW, RetrievedBy.HOST), 33, -84, 10, "permission-ref")
        self.repo.store_observation("location", location, retrieved_at=NOW)
        self.assertEqual(len(self.repo.observations(None)), 1)

    def test_all_supported_observation_kinds_preserve_normalized_payload(self):
        p = Provenance("fixture", NOW, RetrievedBy.SIMULATED)
        host = Provenance("host", NOW, RetrievedBy.HOST)
        flight = FlightObservation(FlightIdentity("s1", "NS123", "ATL", "LAX", NOW, "America/New_York"), NOW, False, None, None, p)
        traffic = TrafficEstimate(TrafficRequest("HOME", "ATL", NOW), 40, p)
        parking = ParkingObservation("s1", host, "ATL", ParkingCategory.ECONOMY, Availability.AVAILABLE, 10, 0, "USD")
        rideshare = RideshareObservation("s1", host, "HOME", "ATL", 5, 40)
        for index, observation in enumerate((flight, traffic, parking, rideshare)):
            self.repo.store_observation(str(index), observation, segment_id="s1", retrieved_at=NOW)
        rows = self.repo.observations("s1")
        self.assertEqual({r["observation_type"] for r in rows}, {"FLIGHT", "TRAFFIC", "PARKING", "RIDESHARE"})
        self.assertEqual(json.loads(rows[0]["payload_json"])["departure"], NOW.isoformat())
        self.assertEqual(json.loads(rows[2]["payload_json"])["price"], 0)

    def test_legacy_segment_foreign_key_links_without_rewriting_history(self):
        legacy = ItineraryRepository(self.path)
        try:
            legacy.apply_snapshot(ItineraryAgent(Source()).refresh(), as_of=datetime(2026, 9, 10, 19))
            before = legacy_rows(legacy.connection)
            segment = "fixture/TRIP-001/OUT"
            self.repo.store_observation("legacy", self.security(segment), retrieved_at=NOW, legacy_segment_id=segment)
            self.assertEqual(legacy_rows(legacy.connection), before)
            self.assertEqual(self.repo.observations(segment)[0]["legacy_segment_id"], segment)
        finally:
            legacy.close()

    def test_retrieval_replay_collision_and_sql_foreign_key(self):
        args = dict(segment_id="s1", as_of=NOW, error=self.error)
        self.repo.record_retrieval("r", "HOST", "a", "SECURITY", **args)
        self.repo.record_retrieval("r", "HOST", "a", "SECURITY", **args)
        self.assertEqual(self.count("retrieval_attempts"), 1)
        with self.assertRaises(ValueError):
            self.repo.record_retrieval("r", "OTHER", "a", "SECURITY", **args)
        with self.assertRaises(sqlite3.IntegrityError), self.repo.connection:
            self.repo.connection.execute("INSERT OR REPLACE INTO retrieval_attempts SELECT * FROM retrieval_attempts")

    def test_sql_constraints_and_immutable_replace(self):
        self.repo.store_observation("o1", self.security(), retrieved_at=NOW)
        for sql in ("UPDATE live_observations SET source='other'", "DELETE FROM live_observations",
                    "INSERT OR REPLACE INTO live_observations SELECT * FROM live_observations"):
            with self.assertRaises(sqlite3.IntegrityError), self.repo.connection:
                self.repo.connection.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError), self.repo.connection:
            self.repo.connection.execute("INSERT INTO retrieval_attempts VALUES('r','p','a','SECURITY','s1','time','missing',NULL)")
