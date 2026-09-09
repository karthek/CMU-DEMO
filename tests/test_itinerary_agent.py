import copy
import unittest
from travel_agent.agents.itinerary_agent import ItineraryAgent


def snapshot():
    return {"source_id": "fixture", "time_basis": "America/New_York", "records": [
        {"itinerary_id": "TRIP-001", "segments": [
            {"segment_id": "OUT", "flight_number": "DL1425", "departure_date": "2026-09-11",
             "origin": "ATL", "destination": "PHL", "scheduled_departure": "2026-09-11T19:00", "booking_status": "CONFIRMED"}]}]}


class Source:
    source_id = "fixture"

    def __init__(self, data=None):
        self.data = snapshot() if data is None else data

    def fetch_snapshot(self):
        return copy.deepcopy(self.data)


class AgentTests(unittest.TestCase):
    def test_segment_duplicate_conflict_and_nested_time_basis(self):
        data = snapshot()
        segments = data["records"][0]["segments"]
        segments.append(copy.deepcopy(segments[0]))
        self.assertEqual(ItineraryAgent(Source(data)).refresh().duplicates_ignored, 1)
        segments[1]["origin"] = "JFK"
        self.assertEqual(ItineraryAgent(Source(data)).refresh().diagnostics[0].code, "IDENTITY_COLLISION")
        data = snapshot()
        data["records"][0]["segments"][0]["time_basis"] = "UTC"
        self.assertEqual(ItineraryAgent(Source(data)).refresh().status, "REJECTED")

    def test_ids_escape_and_order_is_not_identity(self):
        from travel_agent.itinerary.contracts import identity
        self.assertNotEqual(identity("a/b", "c"), identity("a", "b/c"))
        data = snapshot()
        second = dict(data["records"][0]["segments"][0], segment_id="AAA")
        data["records"][0]["segments"].append(second)
        first = ItineraryAgent(Source(data)).refresh().itineraries
        data["records"][0]["segments"].reverse()
        self.assertEqual(ItineraryAgent(Source(data)).refresh().itineraries, first)

    def test_fixture_file_missing_and_malformed_json(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from travel_agent.itinerary.source import FixtureItinerarySource
        with TemporaryDirectory() as temp:
            path = Path(temp) / "source.json"
            agent = ItineraryAgent(FixtureItinerarySource(path))
            self.assertEqual(agent.refresh().status, "SOURCE_ERROR")
            path.write_text("{")
            self.assertEqual(agent.refresh().status, "REJECTED")

    def test_normalization_stable_ids(self):
        data = snapshot()
        data["records"][0]["segments"][0]["flight_number"] = " dl1425 "
        result = ItineraryAgent(Source(data)).refresh()
        segment = result.itineraries[0].segments[0]
        self.assertEqual(segment.segment_id, "fixture/TRIP-001/OUT")
        self.assertEqual(segment.flight_number, "DL1425")
        data["records"][0]["segments"][0]["scheduled_departure"] = "2026-09-11T20:00"
        self.assertEqual(ItineraryAgent(Source(data)).refresh().itineraries[0].segments[0].segment_id, segment.segment_id)

    def test_duplicates_and_conflicts(self):
        data = snapshot()
        data["records"] *= 2
        self.assertEqual(ItineraryAgent(Source(data)).refresh().duplicates_ignored, 1)
        data = copy.deepcopy(data)
        data["records"][1] = copy.deepcopy(data["records"][1])
        data["records"][1]["segments"][0]["origin"] = "JFK"
        result = ItineraryAgent(Source(data)).refresh()
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.itineraries, ())
        self.assertEqual(result.diagnostics[0].code, "IDENTITY_COLLISION")

    def test_missing_malformed_and_time_basis(self):
        for change in (lambda d: d.pop("time_basis"), lambda d: d.update(time_basis="UTC"),
                       lambda d: d["records"][0].pop("segments"),
                       lambda d: d["records"][0]["segments"][0].pop("flight_number"),
                       lambda d: d["records"].append(None)):
            data = snapshot()
            change(data)
            self.assertEqual(ItineraryAgent(Source(data)).refresh().status, "REJECTED")

    def test_empty_and_source_failure(self):
        data = snapshot()
        data["records"] = []
        result = ItineraryAgent(Source(data)).refresh()
        self.assertEqual((result.status, result.itineraries), ("APPLIED", ()))
        class Broken(Source):
            def fetch_snapshot(self):
                raise OSError("private")
        result = ItineraryAgent(Broken()).refresh()
        self.assertEqual(result.status, "SOURCE_ERROR")
        self.assertNotIn("private", str(result.to_dict()))

    def test_multisegment_and_distinct_bookings(self):
        data = snapshot()
        second = copy.deepcopy(data["records"][0]["segments"][0])
        second.update(segment_id="RETURN", origin="PHL", destination="ATL", departure_date="2026-09-14", scheduled_departure="2026-09-14T18:00")
        data["records"][0]["segments"].append(second)
        third = copy.deepcopy(second)
        third["segment_id"] = "CONNECT"
        data["records"][0]["segments"].append(third)
        record = copy.deepcopy(data["records"][0])
        record["itinerary_id"] = "TRIP-002"
        data["records"].append(record)
        result = ItineraryAgent(Source(data)).refresh()
        self.assertEqual(len(result.itineraries), 2)
        self.assertEqual(len(result.itineraries[0].segments), 3)
