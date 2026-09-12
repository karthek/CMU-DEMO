import subprocess
import sys
import unittest
from pathlib import Path

from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.baseline import generate_baseline_candidates
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.providers.mock_provider import MockProvider
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = TravelCoordinator(
            FakeFlightTool(), FakeCalendarTool(), BeamSearchPlanner(2, 3),
        )

    def test_v1_baseline_candidates_and_mock_compatibility(self):
        context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        plans = generate_baseline_candidates(context)
        self.assertEqual([p["leave_time"][-5:] for p in plans],
                         ["15:45", "16:15", "16:45", "17:15"])
        self.assertEqual([p["label"] for p in plans],
                         ["Conservative", "Balanced", "Efficient", "Aggressive"])
        self.assertEqual(MockProvider().generate_candidate_plans(context), plans)
        self.assertEqual(MockProvider().generate_candidate_plans(context, 2), plans[:2])

    def test_v6_recommendation_and_distinct_finalists(self):
        result = self.coordinator.plan_trip("DL1425", "2026-09-11")
        self.assertEqual(result["recommendation"],
            "Leave at 16:25 for flight DL1425. Selected 'Balanced / later' "
            "with score 77.20. The planner balanced airport buffer against workday impact.")
        self.assertEqual([p["leave_time"] for p in result["finalists"]],
                         ["2026-09-11T16:25", "2026-09-11T16:15"])
        context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual(MockProvider().summarize_recommendation(context, result["finalists"]),
                         result["recommendation"])

    def test_demo_runs_with_provider_and_sdk_imports_blocked(self):
        script = '''
import importlib.abc
import runpy
import sys
class BlockProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("travel_agent.providers", "openai", "anthropic", "google", "boto3")):
            raise ImportError("Blocked model dependency: " + fullname)
sys.meta_path.insert(0, BlockProviders())
runpy.run_module("app", run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Leave at 16:25", result.stdout)
