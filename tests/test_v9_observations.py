from dataclasses import replace
from datetime import timedelta
import unittest
from travel_agent.live.observations import (DataStatus, FreshnessPolicy, LiveObservation, ObservationTable as Table,
    Provenance, RetrievedBy, freshness, parse_host_row, required_host_input)
from travel_agent.live.time import parse_instant


NOW = parse_instant("2026-09-11T12:00Z")


def row(**updates):
    result = dict(segment_id="s1", airport="ATL", screening_type="STANDARD", wait_minutes=10,
                  observed_at="2026-09-11T12:00Z", source="airport-page", retrieved_by="HOST")
    result.update(updates)
    return result


def parse(table, data, **kwargs):
    return parse_host_row(table, data, as_of=NOW, segment_id="s1", airport_code="ATL", **kwargs)


class V9ObservationTests(unittest.TestCase):
    def test_table_type_cannot_suppress_a_required_request(self):
        security = parse(Table.SECURITY, row())
        with self.assertRaises(ValueError):
            required_host_input("s1", frozenset({Table.PARKING}), frozenset(), {Table.PARKING: security}, as_of=NOW)

    def test_security_typed_and_exact_freshness_boundary(self):
        good = parse(Table.SECURITY, row(observed_at="2026-09-11T11:00Z"))
        self.assertEqual(freshness(good, NOW).status, DataStatus.USABLE)
        self.assertEqual(freshness(good, NOW + timedelta(microseconds=1)).status, DataStatus.DATA_UNAVAILABLE)
        self.assertEqual(freshness(good, NOW, FreshnessPolicy(59)).status, DataStatus.DATA_UNAVAILABLE)

    def test_last_good_survives_failed_retrieval(self):
        good = parse(Table.SECURITY, row())
        result = freshness(good, NOW, retrieval_failure_code="TIMEOUT")
        self.assertEqual(result.status, DataStatus.USABLE)
        self.assertEqual(result.retrieval_failure_code, "TIMEOUT")
        self.assertEqual(freshness(None, NOW).reason, "MISSING")

    def test_invalid_numeric_and_override_fields(self):
        for update in ({"wait_minutes": True}, {"wait_minutes": -1}, {"wait_minutes": float("nan")},
                       {"wait_minutes": float("inf")}, {"feasible": True}, {"score": 100}, {"approved": True},
                       {"as_of": "2026-09-11T12:00Z"}, {"provenance": {}}, {"retrieved_by": "PROVIDER"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                parse(Table.SECURITY, row(**update))

    def test_wrong_segment_airport_future_naive(self):
        for update in ({"segment_id": "s2"}, {"airport": "PHL"}, {"observed_at": "2026-09-11T12:01Z"},
                       {"observed_at": "2026-09-11T12:00"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                parse(Table.SECURITY, row(**update))

    def test_parking_categories_and_money(self):
        data = row()
        data.pop("screening_type")
        data.pop("wait_minutes")
        data.update(category="ECONOMY", availability="AVAILABLE", parking_to_terminal_minutes=10, price=15, currency="USD")
        self.assertEqual(parse(Table.PARKING, data).price, 15)
        for category in ("DAILY", "TERMINAL/GARAGE"):
            self.assertEqual(parse(Table.PARKING, dict(data, category=category)).category, category)
        with self.assertRaises(ValueError):
            parse(Table.PARKING, dict(data, category="OFF_AIRPORT"))
        with self.assertRaises(ValueError):
            parse(Table.PARKING, dict(data, currency=None))

    def test_rideshare_and_authorized_optional_location(self):
        data = row()
        data.pop("screening_type")
        data.pop("wait_minutes")
        data.update(origin="HOME", pickup_eta_minutes=3, travel_minutes=40, estimated_fare=20, currency="USD")
        self.assertEqual(parse(Table.RIDESHARE, data).travel_minutes, 40)
        location = {k: v for k, v in row().items() if k in ("segment_id", "observed_at", "source", "retrieved_by")}
        location.update(segment_id=None, latitude=33.6, longitude=-84.4, precision_meters=20, permission_reference="p1")
        with self.assertRaises(ValueError):
            parse(Table.LOCATION, location)
        self.assertIsNone(parse(Table.LOCATION, location, authorized_location_permissions=frozenset({"p1"})).segment_id)
        with self.assertRaises(ValueError):
            parse(Table.LOCATION, dict(location, latitude=91), authorized_location_permissions=frozenset({"p1"}))

    def test_no_request_for_fresh_rows(self):
        good = parse(Table.SECURITY, row())
        required = frozenset({Table.SECURITY})
        self.assertIsNone(required_host_input("s1", required, frozenset({Table.LOCATION}), {Table.SECURITY: good}, as_of=NOW))
        request = required_host_input("s1", required, frozenset(), {Table.SECURITY: good}, as_of=NOW + timedelta(minutes=61))
        self.assertEqual(request.required_tables, (Table.SECURITY,))
        self.assertEqual(request.freshness_minutes, 60)

    def test_simulated_cannot_satisfy_live_and_future_not_usable(self):
        provenance = Provenance("fixture", NOW, RetrievedBy.SIMULATED)
        simulated = LiveObservation("o1", "s1", 0, provenance)
        self.assertEqual(freshness(simulated, NOW).reason, "SIMULATED_NOT_LIVE")
        future = replace(simulated, provenance=Provenance("provider", NOW + timedelta(seconds=1), RetrievedBy.PROVIDER))
        self.assertEqual(freshness(future, NOW).reason, "FUTURE")

    def test_invalid_freshness_policy(self):
        for value in (-1, True, 1.5, "60", None):
            with self.assertRaises(ValueError):
                FreshnessPolicy(value)
