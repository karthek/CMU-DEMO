from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from travel_agent.itinerary.clock import FixedClock, SystemClock, parse_local, local_zone


class ClockTests(unittest.TestCase):
    def test_fixed_exact(self):
        value = datetime(2026, 9, 10, 19)
        self.assertEqual(FixedClock(value).now(), value)

    def test_system_zone(self):
        value = SystemClock().now()
        self.assertIsNone(value.tzinfo)
        self.assertEqual(str(local_zone()), "America/New_York")

    def test_invalid_local_times(self):
        for value in ("2026-09-11T19:00-04:00", "2026-11-01T01:30", "2026-03-08T02:30", "2026-09-11"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_local(value)

    def test_zone_unavailable(self):
        with patch("travel_agent.itinerary.clock.ZoneInfo", side_effect=ZoneInfoNotFoundError), self.assertRaisesRegex(RuntimeError, "timezone data unavailable"):
            local_zone()
