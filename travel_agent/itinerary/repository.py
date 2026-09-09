"""Transactional snapshot storage and durable planning attempts. Never decides eligibility."""
import json
from pathlib import Path
import sqlite3
from travel_agent.itinerary.clock import TIME_BASIS, parse_local
from travel_agent.itinerary.models import BookedSegment, Diagnostic, PlanningAttempt, RefreshResult


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_runs (
 refresh_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, as_of TEXT NOT NULL,
 time_basis TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('APPLIED','REJECTED','SOURCE_ERROR')),
 counts_json TEXT NOT NULL, diagnostics_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS itineraries (
 itinerary_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_itinerary_id TEXT NOT NULL,
 current_revision INTEGER NOT NULL CHECK(current_revision > 0),
 presence_status TEXT NOT NULL CHECK(presence_status IN ('PRESENT','MISSING_FROM_SOURCE')),
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 last_refresh_id INTEGER NOT NULL REFERENCES refresh_runs(refresh_id),
 UNIQUE(source_id, source_itinerary_id));
CREATE TABLE IF NOT EXISTS itinerary_revisions (
 itinerary_id TEXT NOT NULL REFERENCES itineraries(itinerary_id), revision INTEGER NOT NULL,
 normalized_snapshot_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
 refresh_id INTEGER NOT NULL REFERENCES refresh_runs(refresh_id),
 PRIMARY KEY(itinerary_id, revision));
CREATE TABLE IF NOT EXISTS segments (
 segment_id TEXT PRIMARY KEY, itinerary_id TEXT NOT NULL REFERENCES itineraries(itinerary_id),
 source_segment_id TEXT NOT NULL, flight_number TEXT NOT NULL, departure_date TEXT NOT NULL,
 origin TEXT NOT NULL, destination TEXT NOT NULL, scheduled_departure TEXT NOT NULL,
 booking_status TEXT NOT NULL CHECK(booking_status IN ('CONFIRMED','CANCELLED')),
 presence_status TEXT NOT NULL CHECK(presence_status IN ('PRESENT','MISSING_FROM_SOURCE')),
 current_revision INTEGER NOT NULL,
 FOREIGN KEY(itinerary_id, current_revision) REFERENCES itinerary_revisions(itinerary_id, revision),
 UNIQUE(itinerary_id, source_segment_id));
CREATE TABLE IF NOT EXISTS automatic_planning_state (
 segment_id TEXT NOT NULL REFERENCES segments(segment_id),
 trigger TEXT NOT NULL CHECK(trigger='AUTOMATIC_LEAD_TIME'),
 state TEXT NOT NULL CHECK(state IN ('NOT_ACTIVATED','PLANNING','COMPLETED','FAILED')),
 last_attempt_number INTEGER NOT NULL DEFAULT 0, completed_attempt_number INTEGER,
 updated_at TEXT NOT NULL, PRIMARY KEY(segment_id, trigger),
 CHECK((state='COMPLETED' AND completed_attempt_number IS NOT NULL) OR
       (state!='COMPLETED' AND completed_attempt_number IS NULL)));
CREATE TABLE IF NOT EXISTS planning_attempts (
 attempt_id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES segments(segment_id),
 trigger TEXT NOT NULL CHECK(trigger IN ('AUTOMATIC_LEAD_TIME','USER_REQUEST')),
 activation_mode TEXT NOT NULL CHECK(activation_mode IN ('AUTOMATIC','USER_INITIATED')),
 attempt_number INTEGER NOT NULL CHECK(attempt_number > 0), itinerary_revision INTEGER NOT NULL,
 decision_as_of TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 state TEXT NOT NULL CHECK(state IN ('PLANNING','COMPLETED','FAILED')),
 activation_json TEXT NOT NULL, provenance_json TEXT NOT NULL, context_json TEXT,
 planning_result_json TEXT, error_json TEXT,
 UNIQUE(segment_id, trigger, attempt_number),
 CHECK((trigger='AUTOMATIC_LEAD_TIME' AND activation_mode='AUTOMATIC') OR
       (trigger='USER_REQUEST' AND activation_mode='USER_INITIATED')),
 CHECK((state='PLANNING' AND finished_at IS NULL AND planning_result_json IS NULL AND error_json IS NULL) OR
       (state='COMPLETED' AND finished_at IS NOT NULL AND context_json IS NOT NULL AND planning_result_json IS NOT NULL AND error_json IS NULL) OR
       (state='FAILED' AND finished_at IS NOT NULL AND planning_result_json IS NULL AND error_json IS NOT NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS one_completed_automatic
 ON planning_attempts(segment_id,trigger) WHERE trigger='AUTOMATIC_LEAD_TIME' AND state='COMPLETED';
CREATE UNIQUE INDEX IF NOT EXISTS one_running_automatic
 ON planning_attempts(segment_id,trigger) WHERE trigger='AUTOMATIC_LEAD_TIME' AND state='PLANNING';
CREATE TRIGGER IF NOT EXISTS immutable_revision_replace BEFORE INSERT ON itinerary_revisions
 WHEN EXISTS (SELECT 1 FROM itinerary_revisions
              WHERE itinerary_id=NEW.itinerary_id AND revision=NEW.revision)
 BEGIN SELECT RAISE(ABORT, 'Itinerary revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_revision_update BEFORE UPDATE ON itinerary_revisions
 BEGIN SELECT RAISE(ABORT, 'Itinerary revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_revision_delete BEFORE DELETE ON itinerary_revisions
 BEGIN SELECT RAISE(ABORT, 'Itinerary revisions are immutable'); END;
"""


class ItineraryRepository:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def apply_snapshot(self, normalized, *, as_of):
        counts = dict(records_retrieved=normalized.records_retrieved, itineraries_created=0,
                      itineraries_updated=0, itineraries_unchanged=0,
                      duplicates_ignored=normalized.duplicates_ignored, invalid_records=normalized.invalid_records,
                      itineraries_marked_missing=0)
        stamp = as_of.isoformat()
        diagnostics = normalized.diagnostics
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            refresh_id = self.connection.execute(
                "INSERT INTO refresh_runs(source_id,as_of,time_basis,status,counts_json,diagnostics_json) VALUES(?,?,?,?,?,?)",
                (normalized.source_id, stamp, TIME_BASIS, normalized.status, encode(counts),
                 encode([d.to_dict() for d in diagnostics]))).lastrowid
            if normalized.status == "APPLIED":
                existing = {r["itinerary_id"]: r for r in self.connection.execute(
                    "SELECT * FROM itineraries WHERE source_id=?", (normalized.source_id,))}
                incoming = {i.itinerary_id for i in normalized.itineraries}
                for key, old in existing.items():
                    if key not in incoming:
                        if old["presence_status"] == "PRESENT":
                            counts["itineraries_marked_missing"] += 1
                        self.connection.execute("UPDATE itineraries SET presence_status='MISSING_FROM_SOURCE',last_refresh_id=? WHERE itinerary_id=?", (refresh_id, key))
                        self.connection.execute("UPDATE segments SET presence_status='MISSING_FROM_SOURCE' WHERE itinerary_id=?", (key,))
                for itinerary in normalized.itineraries:
                    key = itinerary.itinerary_id
                    content = encode(itinerary.to_dict())
                    old = existing.get(key)
                    revision = old["current_revision"] if old else 1
                    previous = self.connection.execute(
                        "SELECT normalized_snapshot_json FROM itinerary_revisions WHERE itinerary_id=? AND revision=?",
                        (key, revision)).fetchone() if old else None
                    changed = old is None or previous[0] != content
                    if old is None:
                        counts["itineraries_created"] += 1
                        self.connection.execute("INSERT INTO itineraries VALUES(?,?,?,?,?,?,?,?)",
                                                (key, itinerary.source_id, itinerary.source_itinerary_id, revision,
                                                 "PRESENT", stamp, stamp, refresh_id))
                    else:
                        revision += int(changed)
                        counts["itineraries_updated" if changed or old["presence_status"] != "PRESENT" else "itineraries_unchanged"] += 1
                        self.connection.execute("UPDATE itineraries SET current_revision=?,presence_status='PRESENT',last_seen_at=?,last_refresh_id=? WHERE itinerary_id=?",
                                                (revision, stamp, refresh_id, key))
                    if changed:
                        self.connection.execute("INSERT INTO itinerary_revisions VALUES(?,?,?,?,?)",
                                                (key, revision, content, stamp, refresh_id))
                    self.connection.execute("UPDATE segments SET presence_status='MISSING_FROM_SOURCE' WHERE itinerary_id=?", (key,))
                    for segment in itinerary.segments:
                        row = segment.to_dict()
                        self.connection.execute("""INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(segment_id) DO UPDATE SET flight_number=excluded.flight_number,
                            departure_date=excluded.departure_date,origin=excluded.origin,destination=excluded.destination,
                            scheduled_departure=excluded.scheduled_departure,booking_status=excluded.booking_status,
                            presence_status='PRESENT',current_revision=excluded.current_revision""",
                            (row["segment_id"], row["itinerary_id"], row["source_segment_id"], row["flight_number"],
                             row["departure_date"], row["origin"], row["destination"], row["scheduled_departure"],
                             row["booking_status"], "PRESENT", revision))
            self.connection.execute("UPDATE refresh_runs SET counts_json=? WHERE refresh_id=?", (encode(counts), refresh_id))
            upcoming = self.upcoming_segments(as_of=as_of, source_id=normalized.source_id) if normalized.status == "APPLIED" else ()
        return RefreshResult(normalized.status, normalized.source_id, as_of, **counts,
                             upcoming_segments=upcoming, diagnostics=diagnostics)

    def list_current_segments(self, *, source_id=None):
        rows = self.connection.execute("""SELECT s.* FROM segments s JOIN itineraries i USING(itinerary_id)
            WHERE s.presence_status='PRESENT' AND i.presence_status='PRESENT'
            AND (? IS NULL OR i.source_id=?)
            ORDER BY s.scheduled_departure,s.itinerary_id,s.segment_id""", (source_id, source_id))
        return tuple(BookedSegment(**{k: parse_local(row[k]) if k == "scheduled_departure" else row[k]
                                     for k in BookedSegment.__dataclass_fields__}) for row in rows)

    def upcoming_segments(self, *, as_of, source_id=None):
        return tuple(s for s in self.list_current_segments(source_id=source_id) if s.booking_status == "CONFIRMED" and s.scheduled_departure > as_of)

    def resolve_upcoming(self, selector, *, as_of, source_id=None):
        return tuple(s for s in self.upcoming_segments(as_of=as_of, source_id=source_id)
                     if all(getattr(s, key) == value for key, value in selector.items()))

    def revision(self, segment_id):
        return self.connection.execute("SELECT current_revision FROM segments WHERE segment_id=?", (segment_id,)).fetchone()[0]

    def automatic_state(self, segment_id):
        row = self.connection.execute("SELECT * FROM automatic_planning_state WHERE segment_id=? AND trigger='AUTOMATIC_LEAD_TIME'", (segment_id,)).fetchone()
        return dict(row) if row else dict(segment_id=segment_id, trigger="AUTOMATIC_LEAD_TIME",
                                         state="NOT_ACTIVATED", last_attempt_number=0,
                                         completed_attempt_number=None, updated_at=None)

    def initialize_automatic_state(self, segment_id, *, as_of):
        with self.connection:
            self.connection.execute("""INSERT OR IGNORE INTO automatic_planning_state
                (segment_id,trigger,state,updated_at) VALUES(?,'AUTOMATIC_LEAD_TIME','NOT_ACTIVATED',?)""",
                (segment_id, as_of.isoformat()))

    def start_attempt(self, segment, activation, provenance):
        trigger = provenance.trigger
        stamp = provenance.as_of.isoformat()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if trigger == "AUTOMATIC_LEAD_TIME" and self.automatic_state(segment.segment_id)["state"] not in ("NOT_ACTIVATED", "FAILED"):
                raise RuntimeError("Automatic attempt cannot start in current state")
            number = self.connection.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM planning_attempts WHERE segment_id=? AND trigger=?",
                                             (segment.segment_id, trigger)).fetchone()[0]
            attempt_id = self.connection.execute("""INSERT INTO planning_attempts
                (segment_id,trigger,activation_mode,attempt_number,itinerary_revision,decision_as_of,started_at,state,activation_json,provenance_json)
                VALUES(?,?,?,?,?,?,?,'PLANNING',?,?)""",
                (segment.segment_id, trigger, provenance.activation_mode, number, provenance.itinerary_revision,
                 stamp, stamp, encode(activation.to_dict()), encode(provenance.to_dict()))).lastrowid
            if trigger == "AUTOMATIC_LEAD_TIME":
                self.connection.execute("UPDATE automatic_planning_state SET state='PLANNING',last_attempt_number=?,updated_at=? WHERE segment_id=? AND trigger=?",
                                        (number, stamp, segment.segment_id, trigger))
        return attempt_id

    def finish_attempt(self, attempt_id, *, finished_at, context=None, result=None, error=None):
        state = "FAILED" if error is not None else "COMPLETED"
        if error is None and (not isinstance(result, dict) or result.get("status") not in ("PLAN_FOUND", "NO_FEASIBLE_PLAN")):
            raise ValueError("Invalid completed planning result")
        with self.connection:
            row = self.connection.execute("SELECT * FROM planning_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["state"] != "PLANNING":
                raise RuntimeError("Attempt is not running")
            self.connection.execute("UPDATE planning_attempts SET state=?,finished_at=?,context_json=?,planning_result_json=?,error_json=? WHERE attempt_id=?",
                                    (state, finished_at.isoformat(), encode(context) if context is not None else None,
                                     encode(result) if result is not None else None, encode(error) if error is not None else None, attempt_id))
            if row["trigger"] == "AUTOMATIC_LEAD_TIME":
                self.connection.execute("""UPDATE automatic_planning_state SET state=?,completed_attempt_number=?,updated_at=?
                    WHERE segment_id=? AND trigger=?""", (state, row["attempt_number"] if state == "COMPLETED" else None,
                                                          finished_at.isoformat(), row["segment_id"], row["trigger"]))
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id):
        row = self.connection.execute("SELECT * FROM planning_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return PlanningAttempt(**{key: row[key] for key in ("attempt_number", "activation_mode", "trigger", "itinerary_revision",
                                  "decision_as_of", "started_at", "finished_at", "state")},
                               **{key: json.loads(row[column]) if row[column] is not None else None
                                  for key, column in (("activation_result", "activation_json"), ("provenance", "provenance_json"),
                                                      ("context", "context_json"), ("planning_result", "planning_result_json"), ("error", "error_json"))})

    def completed_attempt(self, segment_id):
        row = self.connection.execute("SELECT attempt_id FROM planning_attempts WHERE segment_id=? AND trigger='AUTOMATIC_LEAD_TIME' AND state='COMPLETED'", (segment_id,)).fetchone()
        return self.get_attempt(row[0])

    def recover_interrupted(self, *, as_of):
        # Caller holds the OS lock; no live monitor can own these attempts.
        rows = self.connection.execute("SELECT attempt_id,segment_id FROM planning_attempts WHERE trigger='AUTOMATIC_LEAD_TIME' AND state='PLANNING'").fetchall()
        for row in rows:
            error = Diagnostic("INTERRUPTED_EXECUTION", "Previous automatic execution was interrupted.", segment_id=row["segment_id"])
            self.finish_attempt(row["attempt_id"], finished_at=as_of, error=error.to_dict())
        return len(rows)
