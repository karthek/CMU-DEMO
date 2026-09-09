import copy
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from test_itinerary_agent import Source, snapshot
from travel_agent.agents.itinerary_agent import ItineraryAgent
from travel_agent.itinerary.repository import ItineraryRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.repo = ItineraryRepository(Path(self.temp.name) / "test.sqlite3")
        self.source = Source()
        self.agent = ItineraryAgent(self.source)
        self.now = datetime(2026, 9, 10, 19)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def refresh(self):
        return self.repo.apply_snapshot(self.agent.refresh(), as_of=self.now)

    def test_insert_identical_duplicate(self):
        first = self.refresh()
        self.assertEqual(first.itineraries_created, 1)
        second = self.refresh()
        self.assertEqual(second.itineraries_unchanged, 1)
        self.source.data["records"] *= 2
        third = self.refresh()
        self.assertEqual((third.duplicates_ignored, third.itineraries_unchanged), (1, 1))
        self.assertEqual(self.repo.connection.execute("SELECT count(*) FROM itinerary_revisions").fetchone()[0], 1)

    def test_update_missing_restore_cancel(self):
        self.refresh()
        self.source.data["records"][0]["segments"][0]["scheduled_departure"] = "2026-09-11T20:00"
        self.assertEqual(self.refresh().itineraries_updated, 1)
        self.assertEqual(self.repo.revision("fixture/TRIP-001/OUT"), 2)
        restored = copy.deepcopy(self.source.data)
        self.source.data["records"] = []
        self.assertEqual(self.refresh().itineraries_marked_missing, 1)
        self.assertEqual(self.repo.upcoming_segments(as_of=self.now), ())
        self.source.data = restored
        self.assertEqual(len(self.refresh().upcoming_segments), 1)
        self.source.data["records"][0]["segments"][0]["booking_status"] = "CANCELLED"
        self.assertEqual(self.refresh().upcoming_segments, ())
        self.assertEqual(self.repo.list_current_segments()[0].booking_status, "CANCELLED")

    def test_rejected_batch_preserves_state(self):
        self.refresh()
        self.source.data["records"][0]["segments"][0]["origin"] = "JFK"
        self.source.data["records"].append(None)
        result = self.refresh()
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.upcoming_segments, ())
        self.assertEqual(self.repo.list_current_segments()[0].origin, "ATL")

    def test_transaction_rollback(self):
        self.refresh()
        self.repo.connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON segments BEGIN SELECT RAISE(ABORT,'test'); END")
        self.source.data["records"][0]["segments"][0]["origin"] = "JFK"
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.assertEqual(self.repo.revision("fixture/TRIP-001/OUT"), 1)
        self.assertEqual(self.repo.connection.execute("SELECT count(*) FROM refresh_runs").fetchone()[0], 1)

    def test_schema_and_immutable_history(self):
        self.refresh()
        names = {row[0] for row in self.repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"refresh_runs", "itineraries", "itinerary_revisions", "segments", "automatic_planning_state", "planning_attempts"})
        self.assertEqual(self.repo.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Itinerary revisions are immutable"):
            self.repo.connection.execute("UPDATE itinerary_revisions SET normalized_snapshot_json='{}'")
        self.repo.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Itinerary revisions are immutable"):
            self.repo.connection.execute("DELETE FROM itinerary_revisions")
        self.repo.connection.rollback()

    def test_replace_cannot_overwrite_historical_revision(self):
        self.refresh()
        row = dict(self.repo.connection.execute("SELECT * FROM itinerary_revisions").fetchone())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Itinerary revisions are immutable"):
            with self.repo.connection:
                self.repo.connection.execute("INSERT OR REPLACE INTO itinerary_revisions VALUES(?,?,?,?,?)",
                    (row["itinerary_id"], row["revision"], "{}", row["recorded_at"], row["refresh_id"]))
        self.assertEqual(dict(self.repo.connection.execute("SELECT * FROM itinerary_revisions").fetchone()), row)

    def test_order_and_removed_segment(self):
        data = self.source.data
        second = copy.deepcopy(data["records"][0])
        second["itinerary_id"] = "AAA"
        data["records"].append(second)
        rows = self.refresh().upcoming_segments
        self.assertEqual([s.itinerary_id for s in rows], ["fixture/AAA", "fixture/TRIP-001"])
        data["records"][0]["segments"][0]["segment_id"] = "REPLACEMENT"
        self.refresh()
        self.assertEqual(self.repo.connection.execute("SELECT presence_status FROM segments WHERE segment_id='fixture/TRIP-001/OUT'").fetchone()[0], "MISSING_FROM_SOURCE")
