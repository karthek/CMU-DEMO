from travel_agent.agents.errors import InvalidInputError, InvalidDataError, DataUnavailableError, ToolFailureError
import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.models import CalendarEvent
from travel_agent.tools.calendar_tool import FakeCalendarTool


def event(start, end, priority="normal"):
    return CalendarEvent("Meeting", f"2026-09-11T{start}", f"2026-09-11T{end}", priority)


class CalendarAgentTests(unittest.TestCase):
    def setUp(self):
        self.tool = Mock(spec=["get_events"])
        self.events = FakeCalendarTool().get_events("2026-09-11")
        self.tool.get_events.return_value = self.events
        self.agent = CalendarAgent(self.tool)

    def test_simulated_retrieval(self):
        result = CalendarAgent(FakeCalendarTool()).get_events("2026-09-11")
        self.assertEqual([e.title for e in result], ["Project Review", "Leadership Meeting"])
        self.assertEqual([e.priority for e in result], ["normal", "high"])

    def test_order_and_priority_preservation(self):
        self.tool.get_events.return_value = [event("16:00", "17:00", "critical"), event("10:00", "11:00", "low")]
        result = self.agent.get_events("2026-09-11")
        self.assertEqual([e.priority for e in result], ["low", "critical"])
        self.assertEqual(result[0].start, "2026-09-11T10:00:00")

    def test_invalid_event_times(self):
        for changes in ({"start": "bad"}, {"end": None}, {"start": "2026-09-11"},
                        {"start": "2026-09-11T14:00Z"},
                        {"end": "2026-09-11T14:00"}, {"end": "2026-09-11T13:00"}):
            with self.subTest(changes=changes), self.assertRaises(InvalidDataError):
                self.tool.get_events.return_value = [replace(self.events[0], **changes)]
                self.agent.get_events("2026-09-11")

    def test_invalid_title_or_priority(self):
        for field in ("title", "priority"):
            self.tool.get_events.return_value = [replace(self.events[0], **{field: " "})]
            with self.subTest(field=field), self.assertRaises(InvalidDataError):
                self.agent.get_events("2026-09-11")

    def test_last_meeting_uses_maximum_end_not_last_start(self):
        self.assertEqual(self.agent.last_meeting_end(self.events), "2026-09-11T17:00:00")
        self.assertEqual(self.agent.last_meeting_end([event("09:00", "18:00"), event("16:00", "17:00")]),
                         "2026-09-11T18:00:00")
        self.assertIsNone(self.agent.last_meeting_end([]))

    def test_departure_conflict_boundaries(self):
        for time, count in [("16:29", 0), ("16:30", 1), ("16:45", 1), ("17:00", 0)]:
            with self.subTest(time=time):
                self.assertEqual(len(self.agent.departure_conflicts(self.events, f"2026-09-11T{time}")), count)
        with self.assertRaises(InvalidInputError):
            self.agent.departure_conflicts(self.events, "bad")

    def test_free_windows_merge_overlapping_and_adjacent_events(self):
        meetings = [event("10:30", "12:00"), event("09:00", "11:00"),
                    event("12:00", "13:00"), event("10:00", "10:15")]
        self.assertEqual(self.agent.free_windows(meetings, "2026-09-11T08:00", "2026-09-11T18:00"), [
            ("2026-09-11T08:00:00", "2026-09-11T09:00:00"),
            ("2026-09-11T13:00:00", "2026-09-11T18:00:00"),
        ])

    def test_free_windows_empty_full_clipped_and_outside(self):
        start, end = "2026-09-11T10:00", "2026-09-11T12:00"
        expected = [(start + ":00", end + ":00")]
        self.assertEqual(self.agent.free_windows([], start, end), expected)
        self.assertEqual(self.agent.free_windows([event("08:00", "13:00")], start, end), [])
        self.assertEqual(self.agent.free_windows([event("08:00", "09:00"), event("13:00", "14:00")], start, end), expected)
        self.assertEqual(self.agent.free_windows([event("09:00", "11:00")], start, end),
                         [("2026-09-11T11:00:00", end + ":00")])
        with self.assertRaises(InvalidInputError):
            self.agent.free_windows([], end, start)

    def test_relevant_day_includes_overnight_overlap(self):
        overnight = CalendarEvent("Overnight", "2026-09-10T23:00", "2026-09-11T01:00")
        excluded = CalendarEvent("Tomorrow", "2026-09-12T00:00", "2026-09-12T01:00")
        self.tool.get_events.return_value = [excluded, overnight]
        result = self.agent.get_events("2026-09-11")
        self.assertEqual([e.title for e in result], ["Overnight"])

    def test_read_only_surface_and_no_mutation(self):
        original = copy.deepcopy(self.events)
        result = self.agent.get_events("2026-09-11")
        self.agent.last_meeting_end(self.events)
        conflicts = self.agent.departure_conflicts(self.events, "2026-09-11T16:45")
        self.agent.free_windows(self.events, "2026-09-11T09:00", "2026-09-11T18:00")
        self.assertEqual(self.events, original)
        result[0].title = "Changed returned copy"
        conflicts[0].priority = "Changed returned copy"
        self.assertEqual(self.events, original)
        public_methods = {name for name in dir(CalendarAgent)
                          if not name.startswith("_") and callable(getattr(CalendarAgent, name))}
        self.assertEqual(public_methods, {"get_events", "last_meeting_end", "departure_conflicts", "free_windows"})
        self.tool.get_events.assert_called_once_with("2026-09-11")

    def test_invalid_or_unavailable_tool_data(self):
        for value in ({}, [None]):
            self.tool.get_events.return_value = value
            with self.subTest(value=value), self.assertRaises(InvalidDataError):
                self.agent.get_events("2026-09-11")
        self.tool.get_events.side_effect = OSError("offline")
        with self.assertRaises(ToolFailureError):
            self.agent.get_events("2026-09-11")

    def test_invalid_date_before_retrieval(self):
        with self.assertRaises(InvalidInputError):
            self.agent.get_events("tomorrow")
        self.tool.get_events.assert_not_called()
