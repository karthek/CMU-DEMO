"""Explicit additive V9 migration; the legacy repository remains unchanged."""
from hashlib import sha256
from pathlib import Path
import sqlite3

from travel_agent.itinerary.repository import SCHEMA as V8_SCHEMA
from travel_agent.live.time import utc


V9_STATEMENTS = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK(version > 0), checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""",
    """CREATE TABLE provider_sync_state (
        provider TEXT NOT NULL, account_id TEXT NOT NULL, cursor TEXT,
        last_success_at TEXT, last_attempt_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('SUCCESS','ERROR')), error_json TEXT,
        PRIMARY KEY(provider,account_id),
        CHECK((status='SUCCESS' AND last_success_at IS NOT NULL AND error_json IS NULL)
           OR (status='ERROR' AND error_json IS NOT NULL)))""",
    """CREATE TABLE mail_messages (
        provider TEXT NOT NULL, account_id TEXT NOT NULL, message_id TEXT NOT NULL,
        version TEXT NOT NULL, received_at TEXT NOT NULL, stored_at TEXT NOT NULL,
        message_json TEXT NOT NULL, extraction_json TEXT NOT NULL,
        PRIMARY KEY(provider,account_id,message_id,version))""",
    """CREATE TABLE live_observations (
        observation_id TEXT PRIMARY KEY NOT NULL, observation_type TEXT NOT NULL
          CHECK(observation_type IN ('SECURITY','PARKING','RIDESHARE','LOCATION','FLIGHT','TRAFFIC')),
        segment_id TEXT, legacy_segment_id TEXT REFERENCES segments(segment_id),
        payload_json TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
        retrieved_at TEXT NOT NULL, retrieved_by TEXT NOT NULL
          CHECK(retrieved_by IN ('HOST','PROVIDER','SIMULATED')),
        CHECK(legacy_segment_id IS NULL OR (segment_id IS NOT NULL AND segment_id=legacy_segment_id)))""",
    """CREATE TABLE retrieval_attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL, account_id TEXT NOT NULL,
        observation_type TEXT NOT NULL
          CHECK(observation_type IN ('SECURITY','PARKING','RIDESHARE','LOCATION','FLIGHT','TRAFFIC')),
        segment_id TEXT, attempted_at TEXT NOT NULL,
        observation_id TEXT REFERENCES live_observations(observation_id), error_json TEXT,
        CHECK((observation_id IS NOT NULL AND error_json IS NULL)
           OR (observation_id IS NULL AND error_json IS NOT NULL)))""",
    "CREATE INDEX observations_by_segment_time ON live_observations(segment_id,observation_type,observed_at)",
)


def _immutable(table):
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Immutable evidence'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Immutable evidence'); END",
    )


# REPLACE can bypass delete triggers unless recursive_triggers is enabled. Guard
# duplicate insertion independently, including connections opened by other code.
V9_STATEMENTS += tuple(statement for table in ("mail_messages", "live_observations", "retrieval_attempts")
                       for statement in _immutable(table))
V9_STATEMENTS += (
    """CREATE TRIGGER mail_messages_no_replace BEFORE INSERT ON mail_messages
       WHEN EXISTS(SELECT 1 FROM mail_messages WHERE provider=NEW.provider AND account_id=NEW.account_id
         AND message_id=NEW.message_id AND version=NEW.version)
       BEGIN SELECT RAISE(ABORT,'Immutable evidence'); END""",
    """CREATE TRIGGER live_observations_no_replace BEFORE INSERT ON live_observations
       WHEN EXISTS(SELECT 1 FROM live_observations WHERE observation_id=NEW.observation_id)
       BEGIN SELECT RAISE(ABORT,'Immutable evidence'); END""",
    """CREATE TRIGGER retrieval_attempts_no_replace BEFORE INSERT ON retrieval_attempts
       WHEN EXISTS(SELECT 1 FROM retrieval_attempts WHERE attempt_id=NEW.attempt_id)
       BEGIN SELECT RAISE(ABORT,'Immutable evidence'); END""",
)
MIGRATION_CHECKSUM = sha256("\n".join(V9_STATEMENTS).encode()).hexdigest()


def schema_signature(connection):
    return {(kind, name): " ".join(sql.split()) for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def _legacy_statements():
    # sqlite3.complete_statement handles semicolons inside the V8 trigger bodies.
    pending = ""
    for line in V8_SCHEMA.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise ValueError("Incomplete legacy schema")


def expected_schema(*, current):
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(V8_SCHEMA)
        if current:
            for statement in V9_STATEMENTS:
                connection.execute(statement)
        return schema_signature(connection)
    finally:
        connection.close()


def migrate(connection, *, as_of, backup_path: Path | None = None):
    """Accept only empty, exact V8, or this exact versioned V9 schema.

    Backup is optional and explicit; rollback is unconditional on failure. This
    function owns its transaction and refuses a caller's outstanding transaction.
    No existing application row or timestamp is rewritten.
    """
    stamp = utc(as_of).isoformat()
    if connection.in_transaction:
        raise ValueError("Migration requires an idle connection")
    connection.execute("PRAGMA foreign_keys=ON")
    if backup_path is not None:
        # Exclusive file creation prevents accidentally overwriting another backup.
        path = Path(backup_path)
        with path.open("xb"):
            pass
        backup = sqlite3.connect(path)
        try:
            connection.backup(backup)
        finally:
            backup.close()
    try:
        connection.execute("BEGIN IMMEDIATE")
        actual = schema_signature(connection)
        legacy = expected_schema(current=False)
        current = expected_schema(current=True)
        if actual == current:
            versions = [tuple(row) for row in connection.execute("SELECT version,checksum FROM schema_migrations ORDER BY version")]
            if versions != [(1, MIGRATION_CHECKSUM)]:
                raise ValueError("Unknown or altered migration history")
        elif actual == legacy or not actual:
            if not actual:
                for statement in _legacy_statements():
                    connection.execute(statement)
            for statement in V9_STATEMENTS:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations VALUES(1,?,?)", (MIGRATION_CHECKSUM, stamp))
        else:
            raise ValueError("Unrecognized database schema; migration refused")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Foreign key violations; migration refused")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
