"""Opt-in migration 2. Migration 1 SQL/checksum and legacy entry point stay frozen."""
from hashlib import sha256
from pathlib import Path
import sqlite3

from travel_agent.live.migrations import (V9_STATEMENTS, MIGRATION_CHECKSUM,
    _legacy_statements, expected_schema, schema_signature)
from travel_agent.live.time import utc


STATEMENTS = (
    """CREATE TABLE canonical_bookings (
        booking_id TEXT PRIMARY KEY NOT NULL, carrier TEXT NOT NULL,
        booking_reference TEXT NOT NULL, traveler_reference TEXT NOT NULL,
        UNIQUE(carrier,booking_reference,traveler_reference))""",
    """CREATE TABLE canonical_segments (
        segment_id TEXT PRIMARY KEY NOT NULL, booking_id TEXT NOT NULL REFERENCES canonical_bookings,
        segment_reference TEXT NOT NULL, UNIQUE(booking_id,segment_reference))""",
    """CREATE TABLE booking_events (
        event_id TEXT PRIMARY KEY NOT NULL, segment_id TEXT NOT NULL REFERENCES canonical_segments,
        event_json TEXT NOT NULL, UNIQUE(segment_id,event_json))""",
    """CREATE TABLE booking_event_evidence (
        provider TEXT NOT NULL, account_id TEXT NOT NULL, message_id TEXT NOT NULL, version TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES booking_events,
        PRIMARY KEY(provider,account_id,message_id,version),
        FOREIGN KEY(provider,account_id,message_id,version) REFERENCES mail_messages)""",
    """CREATE TABLE canonical_segment_revisions (
        revision_id TEXT PRIMARY KEY NOT NULL, segment_id TEXT NOT NULL REFERENCES canonical_segments,
        projection_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(segment_id,revision_id))""",
    """CREATE TABLE revision_events (
        revision_id TEXT NOT NULL REFERENCES canonical_segment_revisions,
        event_id TEXT NOT NULL REFERENCES booking_events, PRIMARY KEY(revision_id,event_id))""",
    """CREATE TABLE canonical_current (
        segment_id TEXT PRIMARY KEY NOT NULL REFERENCES canonical_segments, revision_id TEXT NOT NULL,
        FOREIGN KEY(segment_id,revision_id) REFERENCES canonical_segment_revisions(segment_id,revision_id))""",
    """CREATE TABLE mail_sync_runs (
        run_id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL, account_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('FULL','DELTA')), since_at TEXT NOT NULL,
        completed_at TEXT NOT NULL, previous_cursor TEXT, completed_cursor TEXT NOT NULL)""",
    """CREATE TABLE mailbox_visibility (
        provider TEXT NOT NULL, account_id TEXT NOT NULL, message_id TEXT NOT NULL,
        visible INTEGER NOT NULL CHECK(visible IN (0,1)), run_id TEXT NOT NULL REFERENCES mail_sync_runs,
        PRIMARY KEY(provider,account_id,message_id))""",
    """CREATE TABLE mail_removals (
        run_id TEXT NOT NULL REFERENCES mail_sync_runs, provider TEXT NOT NULL,
        account_id TEXT NOT NULL, message_id TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('PROVIDER_REMOVED','ABSENT_FROM_FULL_SYNC')),
        PRIMARY KEY(run_id,provider,account_id,message_id))""",
    """CREATE TABLE sync_projections (
        run_id TEXT NOT NULL REFERENCES mail_sync_runs,
        segment_id TEXT NOT NULL REFERENCES canonical_segments, revision_id TEXT NOT NULL,
        PRIMARY KEY(run_id,segment_id),
        FOREIGN KEY(segment_id,revision_id) REFERENCES canonical_segment_revisions(segment_id,revision_id))""",
    """CREATE TABLE provider_resync_requirements (
        provider TEXT NOT NULL, account_id TEXT NOT NULL,
        resync_required INTEGER NOT NULL CHECK(resync_required IN (0,1)),
        PRIMARY KEY(provider,account_id),
        FOREIGN KEY(provider,account_id) REFERENCES provider_sync_state)""",
    "CREATE INDEX event_evidence_by_event ON booking_event_evidence(event_id)",
    "CREATE INDEX revisions_by_segment ON canonical_segment_revisions(segment_id,created_at)",
    "CREATE INDEX sync_runs_by_account ON mail_sync_runs(provider,account_id,completed_at)",
)


def guards(table, key):
    predicate = " AND ".join(f"{column}=NEW.{column}" for column in key)
    natural_keys = {"canonical_bookings": ("carrier", "booking_reference", "traveler_reference"),
                    "canonical_segments": ("booking_id", "segment_reference"),
                    "booking_events": ("segment_id", "event_json")}
    if table in natural_keys:
        predicate = "(" + predicate + ") OR (" + " AND ".join(f"{c}=NEW.{c}" for c in natural_keys[table]) + ")"
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Immutable history'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Immutable history'); END",
        f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate}) BEGIN SELECT RAISE(ABORT,'Immutable history'); END",
    )


for _table, _key in (
    ("canonical_bookings", ("booking_id",)), ("canonical_segments", ("segment_id",)),
    ("booking_events", ("event_id",)),
    ("booking_event_evidence", ("provider", "account_id", "message_id", "version")),
    ("canonical_segment_revisions", ("revision_id",)),
    ("revision_events", ("revision_id", "event_id")), ("mail_sync_runs", ("run_id",)),
    ("mail_removals", ("run_id", "provider", "account_id", "message_id")),
    ("sync_projections", ("run_id", "segment_id")),
):
    STATEMENTS += guards(_table, _key)
CHECKSUM = sha256("\n".join(STATEMENTS).encode()).hexdigest()


def projection_schema():
    connection = sqlite3.connect(":memory:")
    try:
        for sql in (*_legacy_statements(), *V9_STATEMENTS, *STATEMENTS):
            connection.execute(sql)
        return schema_signature(connection)
    finally:
        connection.close()


def migrate_projection(connection, *, as_of, backup_path=None):
    stamp = utc(as_of).isoformat()
    if connection.in_transaction:
        raise ValueError("Migration requires an idle connection")
    connection.execute("PRAGMA foreign_keys=ON")
    if backup_path is not None:
        with Path(backup_path).open("xb"):
            pass
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
        finally:
            backup.close()
    try:
        connection.execute("BEGIN IMMEDIATE")
        actual = schema_signature(connection)
        signatures = (expected_schema(current=False), expected_schema(current=True), projection_schema())
        if actual and actual not in signatures:
            raise ValueError("Unrecognized database schema")
        version = signatures.index(actual) if actual else -1
        if version >= 1:
            ledger = [tuple(row) for row in connection.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")]
            if ledger != [(1, MIGRATION_CHECKSUM), (2, CHECKSUM)][:version]:
                raise ValueError("Unknown or altered migration history")
        if version == -1:
            for sql in _legacy_statements():
                connection.execute(sql)
        if version < 1:
            for sql in V9_STATEMENTS:
                connection.execute(sql)
            connection.execute("INSERT INTO schema_migrations VALUES(1,?,?)", (MIGRATION_CHECKSUM, stamp))
        if version < 2:
            for sql in STATEMENTS:
                connection.execute(sql)
            connection.execute("INSERT INTO schema_migrations VALUES(2,?,?)", (CHECKSUM, stamp))
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Foreign key violations")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
