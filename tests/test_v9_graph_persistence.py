"""Graph retrieval through the unchanged synchronization/projector transaction."""
from dataclasses import replace
from datetime import timedelta
import inspect
import json

import pytest

from test_v9_graph_client import BINDING, Response, no_network
from test_v9_outlook import MailboxTransport, message, source
from test_v9_mail_sync import NOW, TRAVELERS, mail, OfflineMailSource, page
from travel_agent.live.booking import planning_readiness
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.mail_sync import MailSynchronization
from travel_agent.live.providers import ProviderErrorCode as Code


def airline(kind="booking", sequence=1, *, mid=None, received=NOW, parent="inbox"):
    # Reuse explicitly fictional, frozen Phase 4 evidence; not a real airline claim.
    original = mail(kind, sequence=sequence)
    value = message(mid or kind, parent=parent, received=received)
    value["from"]["emailAddress"]["address"] = original.sender
    value["subject"] = original.subject
    value["body"]["content"] = original.text_body
    return value


@pytest.fixture
def repo(tmp_path):
    repository = BookingRepository(tmp_path / "graph.sqlite3", as_of=NOW)
    yield repository
    repository.close()


def service(repo):
    return MailSynchronization(repo, authorized_travelers=TRAVELERS)


def sync(repo, transport, *, full=False, minute=1, projector=None):
    runner = service(repo) if projector is None else MailSynchronization(repo, authorized_travelers=TRAVELERS, projector=projector)
    return runner.run(source(transport, retained=repo.has_retained_mail_identity),
        "OUTLOOK_MAIL", BINDING.account_id, as_of=NOW + timedelta(minutes=minute), full=full)


def count(repo, table):
    return repo.connection.execute("SELECT count(*) FROM " + table).fetchone()[0]


def state(repo):
    return repo.sync_state("OUTLOOK_MAIL", BINDING.account_id)


def test_booking_change_cancellation_pipeline_and_readiness(repo):
    for minute, (kind, sequence) in enumerate((("booking", 1), ("change", 2), ("cancellation", 3)), 1):
        value = airline(kind, sequence)
        result = sync(repo, MailboxTransport({kind: value}), minute=minute)
        assert result.success
        segment, = repo.current_segments()
        assert planning_readiness(segment, authorized_travelers=TRAVELERS).planning_allowed == (kind != "cancellation")
    assert count(repo, "mail_messages") == count(repo, "booking_events") == 3
    assert count(repo, "canonical_segment_revisions") == 3


def test_replay_move_and_removal_preserve_booking_and_evidence(repo):
    value = airline()
    assert sync(repo, MailboxTransport({"booking": value})).success
    original = repo.current_segments()
    value["parentFolderId"] = "archive"
    hints = {"inbox": [{"id": "booking", "@removed": {"reason": "deleted"}}], "archive": [{"id": "booking"}]}
    assert sync(repo, MailboxTransport({"booking": value}, hints), minute=2).success
    assert count(repo, "mail_messages") == count(repo, "booking_events") == 1
    assert repo.current_segments() == original
    removed = MailboxTransport({}, {"archive": [{"id": "booking", "@removed": {"reason": "deleted"}}]})
    assert sync(repo, removed, minute=3).success
    assert repo.current_segments() == original and count(repo, "mail_messages") == 1
    visible = repo.connection.execute("SELECT visible FROM mailbox_visibility WHERE message_id='booking'").fetchone()[0]
    assert visible == 0
    assert planning_readiness(repo.current_segments()[0], authorized_travelers=TRAVELERS).planning_allowed
    assert sync(repo, MailboxTransport({"booking": value}), minute=4).success
    assert count(repo, "mail_messages") == 1


def test_later_booking_does_not_reinstate_cancelled_state(repo):
    for minute, (kind, seq, mid) in enumerate((("booking", 1, "b"), ("cancellation", 2, "c"),
                                             ("booking", 3, "later")), 1):
        assert sync(repo, MailboxTransport({mid: airline(kind, seq, mid=mid)}), minute=minute).success
    assert not planning_readiness(repo.current_segments()[0], authorized_travelers=TRAVELERS).planning_allowed


def test_persisted_multifolder_cursor_survives_reconstruction(repo):
    assert sync(repo, MailboxTransport({"booking": airline()})).success
    before = state(repo)["cursor"]
    assert len(json.loads(before)["folders"]) == 3
    path = repo.path if hasattr(repo, "path") else repo.connection.execute("PRAGMA database_list").fetchone()[2]
    reopened = BookingRepository(path, as_of=NOW + timedelta(minutes=2))
    try:
        assert reopened.sync_state("OUTLOOK_MAIL", BINDING.account_id)["cursor"] == before
        # Receipt has aged outside the new full discovery window; persisted
        # membership, not source memory or version, still admits it.
        transport = MailboxTransport({"booking": airline()})
        result = service(reopened).run(source(transport, retained=reopened.has_retained_mail_identity),
            "OUTLOOK_MAIL", BINDING.account_id, as_of=NOW + timedelta(days=366), full=True)
        assert result.success
        assert count(reopened, "mail_messages") == 1
        assert reopened.has_retained_mail_identity(provider="OUTLOOK_MAIL", account_id=BINDING.account_id, message_id="booking")
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["body", "quiet", "overflow", "projection"])
def test_failure_keeps_prior_evidence_and_checkpoint(repo, failure):
    assert sync(repo, MailboxTransport({"booking": airline()})).success
    before = state(repo)["cursor"]
    transport = MailboxTransport({"new": airline(mid="new")})
    projector = None
    if failure == "body":
        transport.overrides[("messages", "new")] = Response({}, 503)
    elif failure == "quiet":
        transport.overrides[("mailFolders", "inbox", "messages", "delta")] = {
            "value": [{"id": "new"}], "@odata.deltaLink": json.loads(before)["folders"][1]["delta_url"]}
    elif failure == "overflow":
        transport.overrides[("messages", "new")] = Response(b" " * (4 * 1024 * 1024 + 1))
    else:
        def projector(*args, **kwargs):
            raise RuntimeError("offline projection failure")
    result = sync(repo, transport, minute=2, projector=projector)
    assert not result.success and state(repo)["cursor"] == before
    assert count(repo, "mail_messages") == count(repo, "booking_events") == 1


def test_persistent_full_resync_and_qualifying_recovery(repo):
    assert sync(repo, MailboxTransport({"booking": airline()})).success
    before = state(repo)["cursor"]
    expired = MailboxTransport(messages={})
    expired.overrides[("mailFolders", "inbox", "messages", "delta")] = Response({}, 410)
    result = sync(repo, expired, minute=2)
    assert not result.success and result.error.code == Code.CURSOR_EXPIRED and result.resync_required
    path = repo.connection.execute("PRAGMA database_list").fetchone()[2]
    reopened = BookingRepository(path, as_of=NOW + timedelta(minutes=3))
    try:
        assert reopened.resync_required("OUTLOOK_MAIL", BINDING.account_id)
        forbidden = MailboxTransport()
        assert not sync(reopened, forbidden, minute=4).success
        assert not forbidden.calls
        bad_full = MailboxTransport()
        bad_full.overrides[("mailFolders", "archive", "messages", "delta")] = Response({}, 503)
        assert not sync(reopened, bad_full, full=True, minute=5).success
        assert state(reopened)["cursor"] == before
        assert reopened.resync_required("OUTLOOK_MAIL", BINDING.account_id)
        assert sync(reopened, MailboxTransport({"booking": airline()}), full=True, minute=6).success
        assert not reopened.resync_required("OUTLOOK_MAIL", BINDING.account_id)
    finally:
        reopened.close()


def test_cross_provider_evidence_separate_canonical_facts_converge(repo):
    gmail = mail()
    result = service(repo).run(OfflineMailSource(page([gmail])), "GMAIL", "account-1", as_of=NOW + timedelta(minutes=1))
    assert result.success
    assert sync(repo, MailboxTransport({"booking": airline()}), minute=2).success
    assert count(repo, "mail_messages") == 2 and count(repo, "booking_events") == 1
    assert count(repo, "booking_event_evidence") == 2
    assert len(repo.current_segments()) == 1
    assert planning_readiness(repo.current_segments()[0], authorized_travelers=TRAVELERS).planning_allowed
    providers = {r[0] for r in repo.connection.execute("SELECT provider FROM mail_messages")}
    assert providers == {"GMAIL", "OUTLOOK_MAIL"}
    assert repo.sync_state("GMAIL", "account-1")["cursor"] == "cursor-1"
    assert repo.sync_state("OUTLOOK_MAIL", "unrelated-account") is None


def test_future_receipt_rejected_by_existing_atomic_service(repo):
    result = sync(repo, MailboxTransport({"booking": airline(received=NOW + timedelta(days=1))}))
    assert not result.success and count(repo, "mail_messages") == 0
    assert state(repo)["cursor"] is None


def test_provider_modules_have_no_persistence_or_travel_dependency():
    import ast
    from travel_agent.live import outlook, graph_client, graph_normalization, template_registry
    for module in (outlook, graph_client, graph_normalization):
        tree = ast.parse(inspect.getsource(module))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(any(part in name for part in ("sqlite", "booking", "repository", "extraction",
            "template", "planner", "gmail", "reconciliation")) for name in imports if name)
    registry_source = inspect.getsource(template_registry)
    assert "graph_client" not in registry_source and "outlook" not in registry_source
