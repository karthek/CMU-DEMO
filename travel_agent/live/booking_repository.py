"""Atomic offline synchronization checkpoint and auditable canonical projection."""
from datetime import date
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from travel_agent.live.booking import (BookingEvent, CanonicalBooking, CanonicalSegment,
    EventAuthority, Reinstatement, booking_event, project, stable_id,
    booking_lifetime_compatibility, constrain_booking_lifetime)
from travel_agent.live.extraction import ExtractedSegment, ExtractionState, ItineraryExtractor
from travel_agent.live.projection_migrations import migrate_projection
from travel_agent.live.reconciliation import reconcile
from travel_agent.live.repository import LiveRepository, encode
from travel_agent.live.observations import text
from travel_agent.live.time import parse_instant, utc


_CURRENT_CHECKPOINT = object()


def decode_segment(data):
    if data is None:
        return None
    data = dict(data)
    data["departure_date"] = date.fromisoformat(data["departure_date"])
    for key in ("scheduled_departure", "scheduled_arrival"):
        data[key] = parse_instant(data[key]) if data[key] else None
    return ExtractedSegment(**data)


def decode_event(value):
    data = json.loads(value)
    assertion = data.get("reinstatement")
    return BookingEvent(ExtractionState(data["kind"]), decode_segment(data["segment"]),
        EventAuthority(**data["authority"]) if data["authority"] else None, tuple(data["issues"]), data["parser_version"],
        Reinstatement(EventAuthority(**assertion["cancelled_authority"]), assertion["segment_reference"]) if assertion else None)


def encode_event(event):
    data = asdict(event)
    if event.reinstatement is None:
        del data["reinstatement"]  # Preserve existing immutable v1 event bytes/IDs.
    return encode(data)


class BookingRepository(LiveRepository):
    def __init__(self, path, *, as_of, backup_path=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=0)
        self.connection.row_factory = sqlite3.Row
        try:
            migrate_projection(self.connection, as_of=as_of, backup_path=backup_path)
        except BaseException:
            self.connection.close()
            raise

    def complete_sync(self, *args, **kwargs):
        raise ValueError("BookingRepository requires the complete MailSynchronization transaction")

    def store_messages(self, *args, **kwargs):
        raise ValueError("BookingRepository requires the complete MailSynchronization transaction")

    def bookings(self):
        evidence = self._booking_lifetime_evidence()
        return tuple(CanonicalBooking(**dict(row), lifetime_compatibility=booking_lifetime_compatibility(
            event for _, event in evidence.get(row["booking_id"], ()))) for row in self.connection.execute(
            "SELECT * FROM canonical_bookings ORDER BY booking_id"))

    def _booking_lifetime_evidence(self):
        evidence = {}
        for row in self.connection.execute("""SELECT s.booking_id,e.event_id,e.event_json
            FROM canonical_segments s JOIN booking_events e ON e.segment_id=s.segment_id
            ORDER BY s.booking_id,e.event_id"""):
            evidence.setdefault(row["booking_id"], []).append((row["event_id"], decode_event(row["event_json"])))
        return evidence

    def current_segments(self):
        evidence = self._booking_lifetime_evidence()
        rows = self.connection.execute("""SELECT r.projection_json FROM canonical_current c
            JOIN canonical_segment_revisions r ON r.revision_id=c.revision_id ORDER BY c.segment_id""")
        result = []
        for row in rows:
            data = json.loads(row[0])
            data["schedule"] = decode_segment(data["schedule"])
            data["reasons"] = tuple(data["reasons"])
            data["current_authority"] = EventAuthority(**data["current_authority"]) if data["current_authority"] else None
            compatibility = booking_lifetime_compatibility(event for _, event in evidence.get(data["booking_id"], ()))
            result.append(constrain_booking_lifetime(CanonicalSegment(**data), compatibility))
        return tuple(result)

    def history(self, segment_id):
        return tuple(dict(row) for row in self.connection.execute(
            "SELECT * FROM canonical_segment_revisions WHERE segment_id=? ORDER BY created_at,revision_id", (segment_id,)))

    def evidence(self, segment_id):
        return tuple(dict(row) for row in self.connection.execute("""SELECT m.*, e.event_id, e.event_json
            FROM booking_events e JOIN booking_event_evidence l ON l.event_id=e.event_id
            JOIN mail_messages m USING(provider,account_id,message_id,version)
            WHERE e.segment_id=? ORDER BY provider,account_id,message_id,version""", (segment_id,)))

    def unresolved_identity(self, *, authorized_travelers):
        return reconcile(self.extractions(), authorized_travelers=authorized_travelers).unresolved

    def resync_required(self, provider, account_id):
        row = self.connection.execute("SELECT resync_required FROM provider_resync_requirements WHERE provider=? AND account_id=?", (provider, account_id)).fetchone()
        # Preserve cursor-expired evidence when upgrading an existing Phase 3 DB.
        state = self.sync_state(provider, account_id)
        return bool(row and row[0]) or bool(state and state["error_json"] and json.loads(state["error_json"])["code"] == "CURSOR_EXPIRED")

    def sync_failed(self, provider, account_id, *, error, as_of):
        """Phase 3 signature, with migration-2 durable resync semantics."""
        self._record_sync_failure(provider, account_id, expected_state=_CURRENT_CHECKPOINT,
                                  error=error, as_of=as_of)

    def record_sync_failure(self, provider, account_id, *, expected_state, error, as_of):
        """Compare-and-record failure without racing a newer successful checkpoint."""
        self._record_sync_failure(provider, account_id, expected_state=expected_state,
                                  error=error, as_of=as_of)

    def _record_sync_failure(self, provider, account_id, *, expected_state, error, as_of):
        """Both public failure paths share one locked, monotonic flag update."""
        from travel_agent.live.providers import ProviderError
        text(provider)
        text(account_id)
        if not isinstance(error, ProviderError):
            raise ValueError("Typed safe provider error required")
        stamp = utc(as_of).isoformat()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.sync_state(provider, account_id)
            if expected_state is not _CURRENT_CHECKPOINT and current != expected_state:
                return
            if current and parse_instant(current["last_attempt_at"]) > utc(as_of):
                if expected_state is _CURRENT_CHECKPOINT:
                    raise ValueError("Out-of-order synchronization failure")
                return
            needs_resync = self.resync_required(provider, account_id) or error.code == "CURSOR_EXPIRED"
            self.connection.execute("""INSERT INTO provider_sync_state VALUES(?,?,NULL,NULL,?,'ERROR',?)
                ON CONFLICT(provider,account_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                status='ERROR',error_json=excluded.error_json""", (provider, account_id, stamp, encode(error)))
            self.connection.execute("""INSERT INTO provider_resync_requirements VALUES(?,?,?)
                ON CONFLICT(provider,account_id) DO UPDATE SET resync_required=excluded.resync_required""",
                (provider, account_id, int(needs_resync)))

    def _project_all(self, *, authorized_travelers, as_of, projector):
        groups = reconcile(self.extractions(), authorized_travelers=authorized_travelers)
        for group in groups.segments:
            carrier, reference, traveler, segment_reference = group.identity
            booking_id = stable_id(group.identity[:3])
            if not self.connection.execute("SELECT 1 FROM canonical_bookings WHERE booking_id=?", (booking_id,)).fetchone():
                self.connection.execute("INSERT INTO canonical_bookings VALUES(?,?,?,?)", (booking_id, carrier, reference, traveler))
            if not self.connection.execute("SELECT 1 FROM canonical_segments WHERE segment_id=?", (group.canonical_id,)).fetchone():
                self.connection.execute("INSERT INTO canonical_segments VALUES(?,?,?)", (group.canonical_id, booking_id, segment_reference))
            for extraction in group.evidence:
                event = booking_event(extraction)
                payload = encode_event(event)
                event_id = stable_id((group.canonical_id, payload))
                if not self.connection.execute("SELECT 1 FROM booking_events WHERE event_id=?", (event_id,)).fetchone():
                    self.connection.execute("INSERT INTO booking_events VALUES(?,?,?)", (event_id, group.canonical_id, payload))
                identity = extraction.message.identity
                previous = self.connection.execute("""SELECT event_id FROM booking_event_evidence
                    WHERE provider=? AND account_id=? AND message_id=? AND version=?""", identity).fetchone()
                if previous and previous[0] != event_id:
                    raise ValueError("Event extraction changed; explicit reprocessing required")
                if not previous:
                    self.connection.execute("INSERT INTO booking_event_evidence VALUES(?,?,?,?,?)", (*identity, event_id))
        # Persisted events remain authoritative even when mail is absent or traveler
        # configuration changes. Readiness always rechecks the caller's traveler set.
        booking_evidence = self._booking_lifetime_evidence()
        for segment in self.connection.execute("SELECT * FROM canonical_segments ORDER BY segment_id").fetchall():
            rows = self.connection.execute("SELECT event_id,event_json FROM booking_events WHERE segment_id=? ORDER BY event_id", (segment["segment_id"],)).fetchall()
            projection = projector(segment["segment_id"], segment["booking_id"], tuple(decode_event(r[1]) for r in rows))
            if not isinstance(projection, CanonicalSegment) or (projection.segment_id, projection.booking_id) != (segment["segment_id"], segment["booking_id"]):
                raise ValueError("Invalid projection identity")
            related = booking_evidence[segment["booking_id"]]
            compatibility = booking_lifetime_compatibility(event for _, event in related)
            projection = constrain_booking_lifetime(projection, compatibility)
            payload = encode(projection)
            revision_id = stable_id((segment["segment_id"], payload))
            if not self.connection.execute("SELECT 1 FROM canonical_segment_revisions WHERE revision_id=?", (revision_id,)).fetchone():
                self.connection.execute("INSERT INTO canonical_segment_revisions VALUES(?,?,?,?)", (revision_id, segment["segment_id"], payload, utc(as_of).isoformat()))
                # A booking-level collision is supported by sibling-coupon evidence
                # too. Keep those exact immutable event links on the new revision.
                evidence_ids = [event_id for event_id, _ in related] if compatibility.status == "UNRESOLVED" else [r[0] for r in rows]
                self.connection.executemany("INSERT INTO revision_events VALUES(?,?)", ((revision_id, event_id) for event_id in evidence_ids))
            self.connection.execute("""INSERT INTO canonical_current VALUES(?,?) ON CONFLICT(segment_id)
                DO UPDATE SET revision_id=excluded.revision_id""", (segment["segment_id"], revision_id))

    def commit_sync(self, provider, account_id, *, expected_state, completed_cursor,
                    messages, removed_ids, full, since, as_of, authorized_travelers,
                    extractor=None, projector=project):
        """All pages already validated. No source I/O inside this transaction."""
        for value in (provider, account_id, completed_cursor):
            text(value)
        if type(full) is not bool or provider == "UNSPECIFIED":
            raise ValueError("Explicit synchronization scope required")
        if not isinstance(messages, tuple) or not isinstance(removed_ids, frozenset):
            raise ValueError("Immutable batch required")
        for removed_id in removed_ids:
            text(removed_id)
        if removed_ids & {m.message_id for m in messages}:
            raise ValueError("Ambiguous visibility within batch")
        if utc(since) > utc(as_of):
            raise ValueError("Invalid synchronization interval")
        stamp = utc(as_of).isoformat()
        previous_cursor = expected_state["cursor"] if expected_state else None
        run_id = stable_id((provider, account_id, expected_state, completed_cursor,
            full, utc(since).isoformat(), stamp, [m.identity for m in messages], sorted(removed_ids)))
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.sync_state(provider, account_id) != expected_state:
                raise ValueError("Stale synchronization checkpoint")
            if self.resync_required(provider, account_id) and not full:
                raise ValueError("Explicit full resync required")
            if expected_state and parse_instant(expected_state["last_attempt_at"]) > utc(as_of):
                raise ValueError("Out-of-order synchronization")
            if any((m.provider, m.account_id) != (provider, account_id) for m in messages):
                raise ValueError("Message outside synchronization scope")
            self._store_messages(messages, as_of=as_of, extractor=extractor or ItineraryExtractor())
            self._project_all(authorized_travelers=authorized_travelers, as_of=as_of, projector=projector)
            if not self.connection.execute("SELECT 1 FROM mail_sync_runs WHERE run_id=?", (run_id,)).fetchone():
                self.connection.execute("INSERT INTO mail_sync_runs VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, provider, account_id, "FULL" if full else "DELTA", utc(since).isoformat(), stamp, previous_cursor, completed_cursor))
                visible = {m.message_id for m in messages}
                removals = {m: "PROVIDER_REMOVED" for m in removed_ids}
                if full:
                    # Absence describes visibility in this full-sync lookback only.
                    known = {r[0] for r in self.connection.execute("""SELECT DISTINCT message_id FROM mail_messages
                        WHERE provider=? AND account_id=? AND received_at>=?""", (provider, account_id, utc(since).isoformat()))}
                    removals.update({m: "ABSENT_FROM_FULL_SYNC" for m in known - visible - set(removed_ids)})
                for message_id in sorted(visible | set(removals)):
                    self.connection.execute("""INSERT INTO mailbox_visibility VALUES(?,?,?,?,?)
                        ON CONFLICT(provider,account_id,message_id) DO UPDATE SET visible=excluded.visible,run_id=excluded.run_id""",
                        (provider, account_id, message_id, int(message_id in visible), run_id))
                for message_id, reason in sorted(removals.items()):
                    self.connection.execute("INSERT INTO mail_removals VALUES(?,?,?,?,?)", (run_id, provider, account_id, message_id, reason))
                self.connection.execute("INSERT INTO sync_projections SELECT ?,segment_id,revision_id FROM canonical_current", (run_id,))
            self.connection.execute("""INSERT INTO provider_sync_state VALUES(?,?,?,?,?,'SUCCESS',NULL)
                ON CONFLICT(provider,account_id) DO UPDATE SET cursor=excluded.cursor,
                last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,status='SUCCESS',error_json=NULL""",
                (provider, account_id, completed_cursor, stamp, stamp))
            self.connection.execute("""INSERT INTO provider_resync_requirements VALUES(?,?,0)
                ON CONFLICT(provider,account_id) DO UPDATE SET resync_required=
                CASE WHEN ? THEN 0 ELSE provider_resync_requirements.resync_required END""",
                (provider, account_id, full))
