"""Migration-2 rollback, upgrade, immutable history and legacy preservation."""
from datetime import timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from test_v9_mail_extraction import NOW, message
from test_v9_persistence import legacy_rows
from test_itinerary_service import ServiceFixture
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.migrations import MIGRATION_CHECKSUM, schema_signature, expected_schema
from travel_agent.live.projection_migrations import CHECKSUM, migrate_projection, projection_schema
from travel_agent.live.repository import LiveRepository
import test_v9_mail_sync as sync_fixtures


class ProjectionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"

    def test_fresh_and_idempotent_schema(self):
        repo = BookingRepository(self.path, as_of=NOW)
        self.addCleanup(repo.close)
        self.assertEqual(schema_signature(repo.connection), projection_schema())
        before = list(repo.connection.iterdump())
        migrate_projection(repo.connection, as_of=NOW + timedelta(days=1))
        self.assertEqual(list(repo.connection.iterdump()), before)
        self.assertEqual([tuple(r) for r in repo.connection.execute("SELECT version,checksum FROM schema_migrations")], [(1, MIGRATION_CHECKSUM), (2, CHECKSUM)])
        self.assertEqual(repo.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_phase3_upgrade_preserves_every_existing_row_and_backup(self):
        old = LiveRepository(self.path, as_of=NOW)
        old.store_messages((message(),), as_of=NOW)
        tables = [r[0] for r in old.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: [tuple(r) for r in old.connection.execute(f"SELECT * FROM {t}")] for t in tables}
        old.close()
        backup = Path(self.temp.name) / "backup.sqlite3"
        repo = BookingRepository(self.path, as_of=NOW + timedelta(days=1), backup_path=backup)
        self.addCleanup(repo.close)
        for table, rows in before.items():
            actual = [tuple(r) for r in repo.connection.execute(f"SELECT * FROM {table}")]
            self.assertEqual(actual[:1] if table == "schema_migrations" else actual, rows)
        connection = sqlite3.connect(backup)
        self.addCleanup(connection.close)
        self.assertEqual(schema_signature(connection), expected_schema(current=True))
        self.assertEqual(repo.current_segments(), ())  # Upgrade never invents travel truth.

    def test_ddl_failure_rolls_back_fresh_and_phase3_databases(self):
        for initialized in (False, True):
            connection = sqlite3.connect(":memory:")
            try:
                if initialized:
                    from travel_agent.live.migrations import migrate
                    migrate(connection, as_of=NOW)
                before = list(connection.iterdump())
                connection.set_authorizer(lambda action, name, *args:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == "canonical_current" else sqlite3.SQLITE_OK)
                with self.assertRaises(sqlite3.DatabaseError):
                    migrate_projection(connection, as_of=NOW)
                connection.set_authorizer(None)
                self.assertEqual(list(connection.iterdump()), before)
            finally:
                connection.close()

    def test_altered_schema_and_both_ledger_checksums_refused(self):
        repo = BookingRepository(self.path, as_of=NOW)
        self.addCleanup(repo.close)
        for version, checksum in ((1, MIGRATION_CHECKSUM), (2, CHECKSUM)):
            with repo.connection:
                repo.connection.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=?", (version,))
            with self.assertRaisesRegex(ValueError, "migration history"):
                migrate_projection(repo.connection, as_of=NOW)
            with repo.connection:
                repo.connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=?", (checksum, version))
        repo.connection.execute("DROP TRIGGER booking_events_no_update")
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            migrate_projection(repo.connection, as_of=NOW)

    def test_foreign_key_corruption_and_pending_transaction_refused(self):
        repo = BookingRepository(self.path, as_of=NOW)
        self.addCleanup(repo.close)
        repo.connection.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "idle"):
            migrate_projection(repo.connection, as_of=NOW)
        repo.connection.rollback()
        repo.connection.execute("PRAGMA foreign_keys=OFF")
        with repo.connection:
            repo.connection.execute("INSERT INTO canonical_segments VALUES('s','missing','coupon')")
        with self.assertRaisesRegex(ValueError, "Foreign key"):
            migrate_projection(repo.connection, as_of=NOW)


class ProjectionLegacyServiceTests(ServiceFixture):
    def test_v8_completed_attempt_and_history_survive_projection_upgrade(self):
        first = self.service.monitor_trips()["dispatches"][0]
        before = legacy_rows(self.service.repository.connection)
        migrate_projection(self.service.repository.connection, as_of=NOW)
        self.assertEqual(legacy_rows(self.service.repository.connection), before)
        second = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual(second["decision"], "SUPPRESS_DUPLICATE")
        self.assertEqual(second["attempt"], first["attempt"])


class ProjectionIntegrityTests(unittest.TestCase):
    def test_history_update_delete_replace_and_foreign_keys(self):
        fixture = sync_fixtures.MailSyncTests()
        fixture.setUp()
        try:
            from test_v9_mail_sync import mail, page
            fixture.sync(mail())
            fixture.sync(pages=(page(removed=("booking",)),))
            connection = fixture.repo.connection
            for table in ("canonical_bookings", "canonical_segments", "booking_events", "booking_event_evidence",
                          "canonical_segment_revisions", "revision_events", "mail_sync_runs", "mail_removals", "sync_projections"):
                column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
                for sql in (f"UPDATE {table} SET {column}={column}", f"DELETE FROM {table}",
                            f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                    with self.assertRaises(sqlite3.IntegrityError), connection:
                        connection.execute(sql)
            with self.assertRaises(sqlite3.IntegrityError), connection:
                connection.execute("INSERT INTO booking_events VALUES('e','missing','{}')")
            with self.assertRaises(sqlite3.IntegrityError), connection:
                connection.execute("INSERT INTO canonical_current VALUES('missing','missing')")
            for table, column in (("canonical_bookings", "booking_id"), ("canonical_segments", "segment_id"), ("booking_events", "event_id")):
                columns = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
                select = ",".join("'alternate-id'" if c == column else c for c in columns)
                with self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute(f"INSERT OR REPLACE INTO {table} SELECT {select} FROM {table}")
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
        finally:
            fixture.doCleanups()
