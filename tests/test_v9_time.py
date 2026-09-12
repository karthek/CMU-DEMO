from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo
from travel_agent.live.time import from_local, legacy_v8_instant, parse_instant, source_instant, utc


class V9TimeTests(unittest.TestCase):
    def test_equivalent_instants(self):
        self.assertEqual(parse_instant("2026-09-11T19:00-04:00"), parse_instant("2026-09-11T23:00Z"))

    def test_no_naive_or_implicit_zone(self):
        with self.assertRaises(ValueError):
            parse_instant("2026-09-11T19:00")
        with self.assertRaises(ValueError):
            from_local(datetime(2026, 9, 11, 19), "")

    def test_gap_rejected(self):
        with self.assertRaises(ValueError):
            from_local(datetime(2026, 3, 8, 2, 30), "America/New_York")
        with self.assertRaises(ValueError):
            utc(datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/New_York")))

    def test_fold_requires_explicit_evidence(self):
        local = datetime(2026, 11, 1, 1, 30)
        with self.assertRaises(ValueError):
            from_local(local, "America/New_York")
        self.assertEqual(from_local(local, "America/New_York", fold=0), datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(from_local(local, "America/New_York", fold=1), datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            from_local(local, "America/New_York", fold=True)

    def test_explicit_legacy_projection(self):
        original = "2026-09-11T19:00"
        self.assertEqual(legacy_v8_instant(original, time_basis="America/New_York"), parse_instant("2026-09-11T23:00Z"))
        self.assertEqual(original, "2026-09-11T19:00")
        with self.assertRaises(ValueError):
            legacy_v8_instant("2026-11-01T01:30", time_basis="America/New_York")

    def test_source_zone_offset_consistency(self):
        self.assertEqual(source_instant("2026-09-11T19:00-04:00", zone_name="America/New_York"), parse_instant("2026-09-11T23:00Z"))
        with self.assertRaises(ValueError):
            source_instant("2026-09-11T19:00-05:00", zone_name="America/New_York")
        self.assertEqual(from_local(datetime(2026, 9, 11, 19), "America/Los_Angeles"), parse_instant("2026-09-12T02:00Z"))
