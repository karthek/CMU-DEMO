"""Provider-neutral evidence/replay contract; no vendor payloads or HTTP fakes."""
from dataclasses import replace
from datetime import timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_v9_mail_extraction import NOW, message
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.mail import MailMessage, mail_evidence_version
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.providers import ProviderError, ProviderErrorCode
from travel_agent.live.repository import decode_message, encode


def versioned(original):
    return replace(original, version=mail_evidence_version(original))


class MailContractTests(unittest.TestCase):
    def test_normalized_equivalent_evidence_and_offset_replay(self):
        original = message()
        equivalent = replace(original, version="another-provisional-version",
            sender="Display Name <" + original.sender + ">",
            subject="  " + original.subject + "  ",
            text_body=original.text_body.replace("\n", "\r\n"),
            received_at=original.received_at.astimezone(timezone(timedelta(hours=2))))
        self.assertEqual(mail_evidence_version(original), mail_evidence_version(equivalent))
        self.assertEqual(mail_evidence_version(versioned(original)), mail_evidence_version(original))
        self.assertEqual(mail_evidence_version(decode_message(json.loads(encode(original)))),
                         mail_evidence_version(original))

    def test_every_retained_evidence_field_affects_version(self):
        original = message()
        changes = [dict(provider="OTHER"), dict(account_id="other-account"),
            dict(message_id="other-message"), dict(sender="other@example.test"),
            dict(subject="Changed subject"), dict(text_body=original.text_body + "\nNew evidence"),
            dict(html_body="<p>Different evidence</p>"), dict(received_at=NOW - timedelta(days=2)),
            dict(thread_id="other-thread"), dict(recipients=("other@example.test",)),
            dict(provenance=Provenance("other-source", NOW, RetrievedBy.SIMULATED))]
        versions = [mail_evidence_version(replace(original, **change)) for change in changes]
        self.assertNotIn(mail_evidence_version(original), versions)
        self.assertEqual(len(set(versions)), len(changes))

    def test_scope_and_provenance_required(self):
        for invalid in (None, replace(message(), provider="UNSPECIFIED"),
                        replace(message(), provenance=None)):
            with self.assertRaises(ValueError):
                mail_evidence_version(invalid)

    def test_legacy_serialization_and_identity_unchanged(self):
        original = message()
        before = encode(original)
        identity = original.identity
        mail_evidence_version(original)
        self.assertEqual(encode(original), before)
        self.assertEqual(original.identity, identity)
        self.assertEqual(set(json.loads(before)), {"account_id", "message_id", "version", "sender",
            "subject", "text_body", "received_at", "html_body", "provider", "thread_id",
            "recipients", "provenance"})

    def test_restart_and_new_checkpoints_do_not_duplicate_or_rekey_booking(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.sqlite3"
            original = message()
            repo = BookingRepository(path, as_of=NOW)
            try:
                def commit(item, stamp, cursor):
                    repo.commit_sync(item.provider, item.account_id,
                        expected_state=repo.sync_state(item.provider, item.account_id),
                        completed_cursor=cursor, messages=(item,), removed_ids=frozenset(),
                        full=False, since=NOW - timedelta(days=365), as_of=stamp,
                        authorized_travelers=frozenset({"TRAVELER-1"}))
                commit(original, NOW, "legacy-checkpoint")
                current = repo.current_segments()
                events = tuple(repo.connection.execute("SELECT event_id FROM booking_events"))
                legacy_json = repo.connection.execute("SELECT message_json FROM mail_messages").fetchone()[0]
                commit(versioned(original), NOW + timedelta(minutes=1), "new-checkpoint")
                self.assertEqual(repo.current_segments(), current)
                self.assertEqual(tuple(repo.connection.execute("SELECT event_id FROM booking_events")), events)
                repo.close()
                repo = BookingRepository(path, as_of=NOW + timedelta(minutes=2))
                commit(versioned(original), NOW + timedelta(minutes=3), "later-checkpoint")
                self.assertEqual(repo.connection.execute("SELECT count(*) FROM mail_messages").fetchone()[0], 2)
                self.assertEqual(repo.connection.execute("SELECT count(*) FROM canonical_segment_revisions").fetchone()[0], 1)
                self.assertEqual(repo.current_segments(), current)
                self.assertEqual(repo.connection.execute("SELECT message_json FROM mail_messages WHERE version=?",
                    (original.version,)).fetchone()[0], legacy_json)
            finally:
                repo.close()

    def test_changed_extraction_evidence_retains_both_versions(self):
        with TemporaryDirectory() as directory:
            repo = BookingRepository(Path(directory) / "evidence.sqlite3", as_of=NOW)
            try:
                first = versioned(message())
                second = versioned(replace(first, text_body=first.text_body.replace("NS123", "NS456")))
                self.assertNotEqual(first.version, second.version)
                repo.commit_sync(first.provider, first.account_id, expected_state=None,
                    completed_cursor="complete", messages=(first, second), removed_ids=frozenset(),
                    full=True, since=NOW - timedelta(days=365), as_of=NOW,
                    authorized_travelers=frozenset({"TRAVELER-1"}))
                self.assertEqual(repo.connection.execute("SELECT count(*) FROM mail_messages").fetchone()[0], 2)
                self.assertEqual(repo.current_segments()[0].status, "UNRESOLVED")
            finally:
                repo.close()

    def test_new_safe_errors_round_trip_and_preserve_resync_requirement(self):
        with TemporaryDirectory() as directory:
            repo = BookingRepository(Path(directory) / "evidence.sqlite3", as_of=NOW)
            try:
                repo.sync_failed("TEST_MAIL", "account", error=ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False), as_of=NOW)
                for code in (ProviderErrorCode.TIMEOUT, ProviderErrorCode.NOT_FOUND,
                             ProviderErrorCode.UNSUPPORTED_CAPABILITY):
                    error = ProviderError(code, code == ProviderErrorCode.TIMEOUT)
                    payload = json.loads(encode(error))
                    self.assertEqual(ProviderError(ProviderErrorCode(payload["code"]),
                        payload["retryable"], payload["retry_after_seconds"]), error)
                    repo.sync_failed("TEST_MAIL", "account", error=error, as_of=NOW)
                    self.assertTrue(repo.resync_required("TEST_MAIL", "account"))
            finally:
                repo.close()
