"""Offline V9 evidence storage. Explicit opt-in; never applies a V8 snapshot."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

from travel_agent.live.extraction import ExtractedSegment, ExtractionResult, ExtractionState, ItineraryExtractor
from travel_agent.live.mail import MailMessage
from travel_agent.live.migrations import migrate
from travel_agent.live.observations import (HostObservation, SecurityObservation, ParkingObservation,
    RideshareObservation, LocationObservation, Provenance, RetrievedBy, text)
from travel_agent.live.providers import FlightIdentity, FlightObservation, TrafficRequest, TrafficEstimate, ProviderError
from travel_agent.live.time import parse_instant, utc


def encode(value):
    def default(item):
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError("Unsupported evidence value")
    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_message(data):
    data = dict(data)
    data["received_at"] = parse_instant(data["received_at"])
    data["recipients"] = tuple(data["recipients"])
    if data["provenance"] is not None:
        p = data["provenance"]
        data["provenance"] = Provenance(p["source"], parse_instant(p["observed_at"]), RetrievedBy(p["retrieved_by"]))
    return MailMessage(**data)


OBSERVATION_TYPES = {SecurityObservation: "SECURITY", ParkingObservation: "PARKING",
    RideshareObservation: "RIDESHARE", LocationObservation: "LOCATION",
    FlightObservation: "FLIGHT", TrafficEstimate: "TRAFFIC"}


class LiveRepository:
    def __init__(self, path, *, as_of, backup_path=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=0)
        self.connection.row_factory = sqlite3.Row
        try:
            migrate(self.connection, as_of=as_of, backup_path=backup_path)
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def sync_state(self, provider, account_id):
        row = self.connection.execute("SELECT * FROM provider_sync_state WHERE provider=? AND account_id=?",
                                      (provider, account_id)).fetchone()
        return dict(row) if row else None

    def _store_messages(self, messages, *, as_of, extractor):
        results = []
        for message in messages:
            if not isinstance(message, MailMessage) or message.provider == "UNSPECIFIED" or message.provenance is None:
                raise ValueError("Provider and provenance required for durable mail")
            if message.received_at > utc(as_of) or message.provenance.observed_at > utc(as_of):
                raise ValueError("Future mail evidence")
            result = extractor.extract(message)
            message_json = encode(message)
            result_json = encode(dict(state=result.state, rule_id=result.rule_id, segment=result.segment, issues=result.issues))
            old = self.connection.execute("""SELECT message_json,extraction_json FROM mail_messages
                WHERE provider=? AND account_id=? AND message_id=? AND version=?""", message.identity).fetchone()
            if old is not None:
                if tuple(old) != (message_json, result_json):
                    raise ValueError("Message/version collision or changed extraction rule; explicit reprocessing required")
            else:
                self.connection.execute("INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?)",
                    (*message.identity, message.received_at.isoformat(), utc(as_of).isoformat(), message_json, result_json))
            results.append(result)
        return tuple(results)

    def store_messages(self, messages, *, as_of, extractor=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            return self._store_messages(messages, as_of=as_of, extractor=extractor or ItineraryExtractor())

    def extractions(self):
        results = []
        for row in self.connection.execute("SELECT message_json,extraction_json FROM mail_messages ORDER BY provider,account_id,message_id,version"):
            message = decode_message(json.loads(row[0]))
            result = json.loads(row[1])
            segment = result["segment"]
            if segment is not None:
                segment["departure_date"] = date.fromisoformat(segment["departure_date"])
                for key in ("scheduled_departure", "scheduled_arrival"):
                    segment[key] = parse_instant(segment[key]) if segment[key] else None
                segment = ExtractedSegment(**segment)
            results.append(ExtractionResult(ExtractionState(result["state"]), message, result["rule_id"], segment, tuple(result["issues"])))
        return tuple(results)

    def complete_sync(self, provider, account_id, *, expected_cursor, completed_cursor, messages, as_of, extractor=None):
        """Commit a fully assembled delta and its extraction evidence atomically.

        Caller must supply ALL pages. Tokens remain opaque and are never used as
        itinerary identity. No mailbox deletion is interpreted as flight cancellation.
        """
        text(provider)
        text(account_id)
        text(completed_cursor)
        stamp = utc(as_of).isoformat()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.sync_state(provider, account_id)
            if (old["cursor"] if old else None) != expected_cursor:
                raise ValueError("Stale synchronization cursor")
            if old and parse_instant(old["last_attempt_at"]) > utc(as_of):
                raise ValueError("Out-of-order synchronization")
            messages = tuple(messages)
            if any((m.provider, m.account_id) != (provider, account_id) for m in messages):
                raise ValueError("Message outside synchronization scope")
            results = self._store_messages(messages, as_of=as_of, extractor=extractor or ItineraryExtractor())
            self.connection.execute("""INSERT INTO provider_sync_state VALUES(?,?,?,?,?,'SUCCESS',NULL)
                ON CONFLICT(provider,account_id) DO UPDATE SET cursor=excluded.cursor,
                last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,
                status='SUCCESS',error_json=NULL""", (provider, account_id, completed_cursor, stamp, stamp))
        return results

    def sync_failed(self, provider, account_id, *, error, as_of):
        text(provider)
        text(account_id)
        if not isinstance(error, ProviderError):
            raise ValueError("Typed safe provider error required")
        stamp = utc(as_of).isoformat()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.sync_state(provider, account_id)
            if old and parse_instant(old["last_attempt_at"]) > utc(as_of):
                raise ValueError("Out-of-order synchronization failure")
            self.connection.execute("""INSERT INTO provider_sync_state VALUES(?,?,NULL,NULL,?,'ERROR',?)
                ON CONFLICT(provider,account_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                status='ERROR',error_json=excluded.error_json""", (provider, account_id, stamp, encode(error)))

    def store_observation(self, observation_id, observation, *, retrieved_at, segment_id=None, legacy_segment_id=None):
        text(observation_id)
        kind = OBSERVATION_TYPES.get(type(observation))
        if kind is None or not isinstance(observation.provenance, Provenance):
            raise ValueError("Supported typed observation required")
        if isinstance(observation, FlightObservation) and not isinstance(observation.identity, FlightIdentity):
            raise ValueError("Typed flight identity required")
        if isinstance(observation, TrafficEstimate) and not isinstance(observation.request, TrafficRequest):
            raise ValueError("Typed traffic request required")
        bound_segment = (observation.segment_id if isinstance(observation, HostObservation) else
                         observation.identity.segment_id if isinstance(observation, FlightObservation) else segment_id)
        if segment_id is not None and bound_segment != segment_id:
            raise ValueError("Segment association conflict")
        if bound_segment is not None:
            text(bound_segment)
        if kind != "LOCATION" and bound_segment is None:
            raise ValueError("Segment association required")
        provenance = observation.provenance
        if provenance.observed_at > utc(retrieved_at):
            raise ValueError("Retrieval predates observation")
        values = (observation_id, kind, bound_segment, legacy_segment_id, encode(observation), provenance.source,
                  provenance.observed_at.isoformat(), utc(retrieved_at).isoformat(), provenance.retrieved_by.value)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.connection.execute("SELECT * FROM live_observations WHERE observation_id=?", (observation_id,)).fetchone()
            if old is not None:
                if tuple(old) != values:
                    raise ValueError("Observation identity collision")
            else:
                self.connection.execute("INSERT INTO live_observations VALUES(?,?,?,?,?,?,?,?,?)", values)

    def observations(self, segment_id):
        return tuple(dict(row) for row in self.connection.execute(
            "SELECT * FROM live_observations WHERE segment_id IS ? ORDER BY observed_at,observation_id", (segment_id,)))

    def record_retrieval(self, attempt_id, provider, account_id, kind, *, segment_id, as_of, observation_id=None, error=None):
        for value in (attempt_id, provider, account_id):
            text(value)
        if (observation_id is None) == (error is None) or (error is not None and not isinstance(error, ProviderError)):
            raise ValueError("Exactly one stored observation or typed error required")
        stamp = utc(as_of).isoformat()
        values = (attempt_id, provider, account_id, kind, segment_id, stamp, observation_id, encode(error) if error else None)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if observation_id is not None:
                row = self.connection.execute("SELECT * FROM live_observations WHERE observation_id=?", (observation_id,)).fetchone()
                if row is None or row["segment_id"] != segment_id or row["observation_type"] != kind or parse_instant(row["retrieved_at"]) > utc(as_of):
                    raise ValueError("Retrieval observation association mismatch")
            old = self.connection.execute("SELECT * FROM retrieval_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old is not None:
                if tuple(old) != values:
                    raise ValueError("Retrieval identity collision")
            else:
                self.connection.execute("INSERT INTO retrieval_attempts VALUES(?,?,?,?,?,?,?,?)", values)
