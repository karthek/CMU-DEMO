import unittest
from travel_agent.providers.mock_provider import MockProvider
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.models import TripContext
from travel_agent.planning.beam_search import BeamSearchPlanner

class PlannerTests(unittest.TestCase):
    def test_planner_returns_two_finalists(self):
        date = "2026-09-11"
        context = TripContext(
            flight=FakeFlightTool().get_flight("DL1425", date),
            calendar_events=FakeCalendarTool().get_events(date),
        )

        candidates = MockProvider().generate_candidate_plans(context, count=4)
        finalists = BeamSearchPlanner(beam_width=2, depth=3).search(context, candidates)

        self.assertEqual(len(finalists), 2)
        self.assertGreaterEqual(finalists[0]["score"], finalists[1]["score"])

if __name__ == "__main__":
    unittest.main()
