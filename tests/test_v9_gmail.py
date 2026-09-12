"""Deterministic fabricated Gmail responses; no account, token or network needed."""
import ast
import base64
from copy import deepcopy
from datetime import timedelta
import inspect
from io import BytesIO
import json
import time

import pytest

from test_v9_mail_sync import mail, NOW, TRAVELERS
from travel_agent.live import gmail
from travel_agent.live.gmail import GmailMailSource, GmailLimits
from travel_agent.live.gmail_client import GmailFailure
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.mail_sync import MailSynchronization
from travel_agent.live.providers import ProviderErrorCode as Code


LOWER = NOW - timedelta(days=365)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


def leaf(value="Generic evidence", mime="text/plain", charset="UTF-8"):
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return dict(mimeType=mime, filename="", headers=[dict(name="Content-Type", value=mime + ("; charset=" + charset if charset else ""))],
                body=dict(size=len(raw), data=base64.urlsafe_b64encode(raw).decode().rstrip("=")))


def message(mid="m1", *, when=NOW, payload=None, labels=()):
    payload = deepcopy(payload or leaf())
    payload.setdefault("headers", []).extend([
        dict(name="From", value="Sender <sender@example.test>"),
        dict(name="Subject", value="Generic evidence"),
        dict(name="To", value="b@example.test, a@EXAMPLE.test"),
        dict(name="Cc", value="a@example.test")])
    return dict(id=mid, internalDate=str(int(when.timestamp() * 1000)), historyId="10",
                threadId="ignored", labelIds=list(labels), payload=payload)


def event(number="11", kind="messagesAdded", mid="m1", labels=None):
    entry = dict(message=dict(id=mid))
    if kind.startswith("labels"):
        entry["labelIds"] = labels or ["TRASH"]
    return dict(id=number, **{kind: [entry]})


class Fake:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def read(self, resource, *, params, timeout, budget):
        self.calls.append((resource, params, timeout))
        expected, response = self.steps.pop(0)
        assert resource == expected
        assert 0 < timeout <= 15
        if isinstance(response, Exception):
            raise response
        # Fabricate explicit wire bytes for this offline client; production never
        # estimates received size by serializing an already-parsed provider dict.
        raw = json.dumps(response, allow_nan=False).encode("utf-8")
        payload = budget.read_body(BytesIO(raw), response_limit=budget.max_response_bytes,
                                   deadline=time.monotonic() + timeout)
        return json.loads(payload)

    def done(self):
        assert not self.steps


def source(client, retained=lambda **kwargs: False, **kwargs):
    return GmailMailSource(account_id="account-1", client=client, retained_identity=retained, **kwargs)


def full_steps(*messages, checkpoint="10"):
    return [("profile", dict(historyId=checkpoint)),
            ("messages", dict(messages=[dict(id=m["id"]) for m in messages])),
            *[("messages/" + m["id"], m) for m in sorted(messages, key=lambda m: m["id"])],
            ("history", dict(historyId=checkpoint)), ("profile", dict(historyId=checkpoint))]


def delta_steps(*records, messages=(), checkpoint="20"):
    return [("history", dict(historyId=checkpoint, history=list(records))),
            *[("messages/" + m["id"], m) for m in sorted(messages, key=lambda m: m["id"])],
            ("profile", dict(historyId=checkpoint))]


def delta(client, *, retained=lambda **kwargs: False, lower=LOWER):
    s = source(client, retained)
    return s.sync(since=NOW, cursor=s._cursor(lower, "10"))


def assert_error(result, code):
    assert result.value is None
    assert result.error.code == code


def test_full_pagination_and_required_full_gets():
    client = Fake(("profile", dict(historyId="10")),
                  ("messages", dict(messages=[dict(id="m2")], nextPageToken="p2")),
                  ("messages", dict(nextPageToken="p3")),
                  ("messages", dict(messages=[dict(id="m1"), dict(id="m2")], resultSizeEstimate=999)),
                  ("messages/m1", message()), ("messages/m2", message("m2")),
                  ("history", dict(historyId="10", nextPageToken="h2")),
                  ("history", dict(historyId="10")), ("profile", dict(historyId="10")))
    result = source(client).sync(since=LOWER, cursor=None)
    assert result.error is None
    assert [m.message_id for m in result.value.messages] == ["m1", "m2"]
    assert result.value.next_page_token is None
    assert json.loads(result.value.completed_cursor)["checkpoint"] == "10"
    assert client.calls[1][1] == dict(q=f"after:{int(LOWER.timestamp()) - 1} -in:drafts", includeSpamTrash="true", maxResults=500)
    assert client.calls[2][1]["pageToken"] == "p2"
    assert client.calls[4][1] == {"format": "full"}
    assert client.calls[7][1]["startHistoryId"] == "10"
    client.done()


def test_full_empty_has_validated_completion_cursor():
    client = Fake(*full_steps())
    result = source(client).sync(since=LOWER, cursor=None)
    assert result.value.messages == ()
    assert result.value.completed_cursor
    client.done()


@pytest.mark.parametrize("retained,expected", [(True, 1), (False, 0)])
def test_old_delta_membership_distinguishes_admission(retained, expected):
    client = Fake(*delta_steps(event(kind="labelsAdded"), messages=[message(when=LOWER - timedelta(days=2))]))
    calls = []
    def lookup(**identity):
        calls.append(identity)
        return retained
    result = delta(client, retained=lookup)
    assert result.error is None
    assert len(result.value.messages) == expected
    assert calls == [dict(provider="GMAIL", account_id="account-1", message_id="m1")]
    client.done()


def test_in_scope_new_admission_does_not_need_membership():
    def unavailable(**kwargs):
        raise AssertionError("In scope does not require membership")
    client = Fake(*delta_steps(event(), messages=[message()]))
    assert len(delta(client, retained=unavailable).value.messages) == 1
    client = Fake(*full_steps(message()))
    assert len(source(client, unavailable).sync(since=LOWER, cursor=None).value.messages) == 1


def test_failed_membership_is_not_absence():
    def unavailable(**kwargs):
        raise RuntimeError("Private lookup detail")
    result = delta(Fake(*delta_steps(event(), messages=[message(when=LOWER - timedelta(days=1))])), retained=unavailable)
    assert_error(result, Code.INVALID_RESPONSE)
    assert "Private" not in repr(result)


def test_restart_persisted_membership_versions_and_scope(tmp_path):
    path = tmp_path / "mail.sqlite3"
    repo = BookingRepository(path, as_of=NOW)
    service = MailSynchronization(repo, authorized_travelers=TRAVELERS)
    old = message(when=LOWER + timedelta(days=1))
    assert service.run(source(Fake(*full_steps(old)), repo.has_retained_mail_identity), "GMAIL", "account-1", as_of=NOW).success
    # Advance the full-scan lower bound past the retained message.
    later = NOW + timedelta(days=4)
    assert service.run(source(Fake(*full_steps())), "GMAIL", "account-1", as_of=later, full=True).success
    repo.close()
    repo = BookingRepository(path, as_of=later)
    try:
        membership = repo.has_retained_mail_identity
        assert membership(provider="GMAIL", account_id="account-1", message_id="m1")
        assert not membership(provider="GMAIL", account_id="other", message_id="m1")
        assert not membership(provider="OUTLOOK_MAIL", account_id="account-1", message_id="m1")
        changed = deepcopy(old)
        changed["payload"]["body"] = leaf("Changed generic evidence")["body"]
        client = Fake(*delta_steps(event(kind="labelsAdded"), event("12", mid="unknown"),
                                  messages=[changed, message("unknown", when=LOWER)]))
        service = MailSynchronization(repo, authorized_travelers=TRAVELERS)
        assert service.run(source(client, membership), "GMAIL", "account-1", as_of=later + timedelta(minutes=1)).success
        assert repo.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 2
        assert repo.connection.execute("SELECT COUNT(DISTINCT version) FROM mail_messages").fetchone()[0] == 2
        assert membership(provider="GMAIL", account_id="account-1", message_id="m1")
        assert not membership(provider="GMAIL", account_id="account-1", message_id="unknown")
    finally:
        repo.close()


def test_source_imports_no_persistence_or_travel_modules():
    imports = [n.module for n in ast.walk(ast.parse(inspect.getsource(gmail))) if isinstance(n, ast.ImportFrom)]
    assert not any(any(word in (name or "") for word in ("repository", "sqlite", "booking", "extraction", "itinerary")) for name in imports)
    assert "sqlite" not in inspect.getsource(gmail)


def test_normalization_and_fingerprint_replay():
    original = message(payload=leaf(" A  B\r\nC "))
    def read(m):
        result = source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None)
        assert result.error is None
        return result.value.messages[0]
    first = read(original)
    assert first.text_body == "A B\nC"
    assert first.recipients == ("a@example.test", "b@example.test")
    assert first.thread_id is None
    assert first.provenance.observed_at == first.received_at
    assert first.version.startswith("mail-evidence/v1:sha256:")
    reread = deepcopy(original)
    reread.update(historyId="555", labelIds=["TRASH", "SPAM"], threadId="changed", snippet="ignored")
    assert read(reread) == first
    reread["payload"]["body"] = leaf("Different body")["body"]
    assert read(reread).version != first.version


def test_history_pagination_coalesces_and_uses_terminal_checkpoint():
    record = event(kind="labelsAdded")
    record["messages"] = [dict(id="m1")]
    record["labelsAdded"] *= 2
    client = Fake(("history", dict(historyId="18", history=[record], nextPageToken="p2")),
                  ("history", dict(historyId="25", history=[record, event("17", kind="labelsRemoved", labels=["INBOX"])])),
                  ("messages/m1", message()), ("profile", dict(historyId="25")))
    result = delta(client)
    assert len(result.value.messages) == 1
    assert json.loads(result.value.completed_cursor)["checkpoint"] == "25"
    assert all(c[1]["startHistoryId"] == "10" for c in client.calls if c[0] == "history")
    client.done()


def test_empty_delta_is_empty_and_keeps_fixed_lower_bound():
    result = delta(Fake(*delta_steps()))
    assert result.value.messages == result.value.removed_message_ids == ()
    assert json.loads(result.value.completed_cursor)["lower_bound"] == LOWER.isoformat()


def test_terminal_deletion_suppresses_earlier_addition_without_fetch():
    result = delta(Fake(*delta_steps(event(), event("15", "messagesDeleted"))))
    assert result.value.removed_message_ids == ("m1",)
    assert result.value.messages == ()


@pytest.mark.parametrize("kind,labels", [("labelsAdded", ["TRASH"]), ("labelsAdded", ["SPAM"]), ("labelsRemoved", ["INBOX"]), ("labelsRemoved", ["UNREAD"])])
def test_label_only_changes_are_not_removals(kind, labels):
    result = delta(Fake(*delta_steps(event(kind=kind, labels=labels), messages=[message(labels=labels)])))
    assert len(result.value.messages) == 1
    assert result.value.removed_message_ids == ()


@pytest.mark.parametrize("payload,code", [
    (dict(messages=None), Code.INVALID_RESPONSE),
    (dict(messages=[{}]), Code.INVALID_RESPONSE),
])
def test_malformed_list(payload, code):
    assert_error(source(Fake(("profile", dict(historyId="10")), ("messages", payload))).sync(since=LOWER, cursor=None), code)


def test_list_cycle_and_bound_fail_without_emitting_partial_page():
    for limits in (GmailLimits(), GmailLimits(max_list_pages=1)):
        client = Fake(("profile", dict(historyId="10")), ("messages", dict(nextPageToken="p")), ("messages", dict(nextPageToken="p")))
        result = source(client, limits=limits).sync(since=LOWER, cursor=None)
        assert result.value is None
        assert result.error.code in (Code.INVALID_RESPONSE, Code.UNSUPPORTED_CAPABILITY)


@pytest.mark.parametrize("cursor", ["garbage", "{}", "[]", "null", '{"format":1,"format":2}'])
def test_invalid_cursor_requires_recovery(cursor):
    client = Fake()
    assert_error(source(client).sync(since=LOWER, cursor=cursor), Code.CURSOR_EXPIRED)
    assert client.calls == []


@pytest.mark.parametrize("field,value", [("account", "other"), ("profile", "different"), ("checkpoint", "bad"), ("lower_bound", "2026-01-01")])
def test_cursor_scope_mismatch(field, value):
    s = source(Fake())
    cursor = json.loads(s._cursor(LOWER, "10"))
    cursor[field] = value
    assert_error(s.sync(since=LOWER, cursor=json.dumps(cursor)), Code.CURSOR_EXPIRED)


def test_full_quiet_window_race_and_disappearing_message():
    steps = full_steps(message())
    steps[-2] = ("history", dict(historyId="11", history=[event()]))
    assert_error(source(Fake(*steps)).sync(since=LOWER, cursor=None), Code.UNAVAILABLE)
    steps = full_steps(message())
    steps[2] = ("messages/m1", GmailFailure(Code.NOT_FOUND))
    assert_error(source(Fake(*steps)).sync(since=LOWER, cursor=None), Code.NOT_FOUND)


def test_incremental_post_fetch_race_and_disappearance():
    steps = delta_steps(event(), messages=[message()])
    steps[-1] = ("profile", dict(historyId="21"))
    assert_error(delta(Fake(*steps)), Code.UNAVAILABLE)
    steps[1] = ("messages/m1", GmailFailure(Code.NOT_FOUND))
    assert_error(delta(Fake(*steps)), Code.NOT_FOUND)


@pytest.mark.parametrize("records", [
    [dict(id="11", **{**event()["messagesAdded"][0]})],
    [event("12"), event("11")],
    [event(), event(kind="messagesDeleted")],
    [event("11", "messagesDeleted"), event("12")],
    [dict(id="11", messages=[dict(id="m1")])],
    [dict(id="11", messagesAdded=event()["messagesAdded"], messagesDeleted=event(kind="messagesDeleted")["messagesDeleted"])],
])
def test_ambiguous_or_malformed_history(records):
    assert_error(delta(Fake(("history", dict(historyId="20", history=records)))), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("mime", ["text/plain", "text/html"])
def test_plain_html_and_line_endings(mime):
    result = source(Fake(*full_steps(message(payload=leaf("A\r\nB\rC", mime))))).sync(since=LOWER, cursor=None)
    value = result.value.messages[0]
    assert (value.text_body if mime == "text/plain" else value.html_body) == "A\nB\nC"


def test_multipart_alternatives_preserve_both_and_skip_attachment():
    payload = dict(mimeType="multipart/mixed", body=dict(size=0), parts=[
        dict(mimeType="multipart/alternative", body=dict(size=0), parts=[leaf("Text"), leaf("<p>HTML</p>", "text/html")]),
        dict(mimeType="application/pdf", filename="synthetic.pdf", body=dict(attachmentId="ignored", size=100))])
    result = source(Fake(*full_steps(message(payload=payload)))).sync(since=LOWER, cursor=None)
    assert result.value.messages[0].text_body == "Text"
    assert result.value.messages[0].html_body == "<p>HTML</p>"


@pytest.mark.parametrize("payload,code", [
    (dict(mimeType="multipart/alternative", body=dict(size=0), parts=[leaf("a"), leaf("b")]), Code.UNSUPPORTED_CAPABILITY),
    (dict(mimeType="message/rfc822", body=dict(size=0)), Code.UNSUPPORTED_CAPABILITY),
    (dict(mimeType="multipart/encrypted", body=dict(size=0)), Code.UNSUPPORTED_CAPABILITY),
    (leaf("a", charset="UTF-16"), Code.UNSUPPORTED_CAPABILITY),
    (leaf(b"\xff", charset="UTF-8"), Code.INVALID_RESPONSE),
    (leaf(b"\xff", charset=None), Code.INVALID_RESPONSE),
    (dict(mimeType="text/plain", body=dict(size=5)), Code.INVALID_RESPONSE),
    (dict(mimeType="application/pdf", filename="a.pdf", body=dict(size=0)), Code.UNSUPPORTED_CAPABILITY),
])
def test_unsupported_or_incomplete_mime(payload, code):
    assert_error(source(Fake(*full_steps(message(payload=payload)))).sync(since=LOWER, cursor=None), code)


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
def test_no_guessed_double_transfer_decoding(encoding):
    payload = leaf()
    payload["headers"].append(dict(name="Content-Transfer-Encoding", value=encoding))
    assert_error(source(Fake(*full_steps(message(payload=payload)))).sync(since=LOWER, cursor=None), Code.UNSUPPORTED_CAPABILITY)


@pytest.mark.parametrize("data,size", [("!!!!", 3), ("Zh", 1), ("Zg=", 1), ("Zg", 5), ("Zg+", 2)])
def test_strict_base64url(data, size):
    payload = leaf()
    payload["body"] = dict(data=data, size=size)
    assert_error(source(Fake(*full_steps(message(payload=payload)))).sync(since=LOWER, cursor=None), Code.INVALID_RESPONSE)


def test_selected_text_attachment_is_fetched_not_truncated():
    payload = leaf()
    body = payload["body"]
    payload["body"] = dict(size=body["size"], attachmentId="body1")
    steps = full_steps(message(payload=payload))
    steps.insert(3, ("messages/m1/attachments/body1", body))
    result = source(Fake(*steps)).sync(since=LOWER, cursor=None)
    assert result.value.messages[0].text_body == "Generic evidence"
    steps[3] = ("messages/m1/attachments/body1", GmailFailure(Code.NOT_FOUND))
    assert_error(source(Fake(*steps)).sync(since=LOWER, cursor=None), Code.NOT_FOUND)


def test_encoded_folded_headers_and_ambiguous_sender():
    m = message()
    m["payload"]["headers"][2]["value"] = "=?utf-8?b?Q2Fmw6k=?=\r\n confirmation"
    result = source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None)
    assert result.value.messages[0].subject == "Café confirmation"
    m["payload"]["headers"].append(dict(name="From", value="other@example.test"))
    assert_error(source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None), Code.INVALID_RESPONSE)


def test_drafts_and_full_overlap_are_excluded():
    client = Fake(*full_steps(message("draft", labels=["DRAFT"]), message("old", when=LOWER - timedelta(milliseconds=1)), message("edge", when=LOWER)))
    result = source(client).sync(since=LOWER, cursor=None)
    assert [m.message_id for m in result.value.messages] == ["edge"]


@pytest.mark.parametrize("limits", [GmailLimits(max_requests=1), GmailLimits(max_mime_bytes=1), GmailLimits(max_batch_bytes=5), GmailLimits(max_response_bytes=5), GmailLimits(max_parts=1, max_depth=1)])
def test_resource_bounds_fail_closed(limits):
    payload = dict(mimeType="multipart/alternative", body=dict(size=0), parts=[leaf()])
    assert_error(source(Fake(*full_steps(message(payload=payload))), limits=limits).sync(since=LOWER, cursor=None), Code.UNSUPPORTED_CAPABILITY)


def test_elapsed_time_and_unexpected_client_exception():
    clock = iter([0, 121]).__next__
    assert_error(source(Fake(), monotonic=clock).sync(since=LOWER, cursor=None), Code.TIMEOUT)
    assert_error(source(Fake(("profile", RuntimeError("private")))).sync(since=LOWER, cursor=None), Code.INVALID_RESPONSE)


def test_expiration_recovery_removal_and_empty_delta_integration(tmp_path):
    repo = BookingRepository(tmp_path / "sync.sqlite3", as_of=NOW)
    try:
        service = MailSynchronization(repo, authorized_travelers=TRAVELERS)
        fixture = mail()
        m = message(payload=leaf(fixture.text_body))
        for h in m["payload"]["headers"]:
            if h["name"] == "Subject":
                h["value"] = fixture.subject
            if h["name"] == "From":
                h["value"] = fixture.sender
        def run(client, minute, full=False):
            return service.run(source(client, repo.has_retained_mail_identity), "GMAIL", "account-1", as_of=NOW + timedelta(minutes=minute), full=full)
        assert run(Fake(*full_steps(m)), 0).success
        before = repo.current_segments()
        assert len(before) == 1
        assert run(Fake(*delta_steps()), 1).success
        assert repo.current_segments() == before
        expired = run(Fake(("history", GmailFailure(Code.CURSOR_EXPIRED))), 2)
        assert expired.resync_required
        blocked = Fake()
        assert run(blocked, 3).resync_required
        assert blocked.calls == []
        assert run(Fake(("profile", GmailFailure(Code.AUTH_REQUIRED))), 4, full=True).resync_required
        assert repo.current_segments() == before
        assert run(Fake(*full_steps(m, checkpoint="30")), 5, full=True).success
        assert not repo.resync_required("GMAIL", "account-1")
        assert run(Fake(*delta_steps(event("31", "messagesDeleted"), checkpoint="40")), 6).success
        assert repo.current_segments() == before
        assert repo.has_retained_mail_identity(provider="GMAIL", account_id="account-1", message_id="m1")
        assert repo.connection.execute("SELECT visible FROM mailbox_visibility").fetchone()[0] == 0
    finally:
        repo.close()


def test_projection_failure_does_not_commit_gmail_cursor_or_evidence(tmp_path):
    repo = BookingRepository(tmp_path / "rollback.sqlite3", as_of=NOW)
    def broken(*args, **kwargs):
        raise RuntimeError("projection failed")
    try:
        # Existing synthetic booking fixture ensures projection is invoked.
        fixture = mail()
        m = message(payload=leaf(fixture.text_body))
        for h in m["payload"]["headers"]:
            if h["name"] == "Subject": h["value"] = fixture.subject
            if h["name"] == "From": h["value"] = fixture.sender
        service = MailSynchronization(repo, authorized_travelers=TRAVELERS, projector=broken)
        result = service.run(source(Fake(*full_steps(m))), "GMAIL", "account-1", as_of=NOW)
        assert not result.success
        assert repo.sync_state("GMAIL", "account-1")["cursor"] is None
        assert not repo.has_retained_mail_identity(provider="GMAIL", account_id="account-1", message_id="m1")
    finally:
        repo.close()


@pytest.mark.parametrize("charset,raw,expected", [(None, b"ASCII", "ASCII"), ("ISO-8859-1", b"caf\xe9", "café"), ("Windows-1252", b"\x80", "€"), ("UTF-8", b"\xef\xbf\xbf", "\uffff")])
def test_allowed_charsets_and_urlsafe_alphabet(charset, raw, expected):
    m = message(payload=leaf(raw, charset=charset))
    result = source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None)
    assert result.value.messages[0].text_body == expected
    # Canonical padded and unpadded encodings yield the same evidence.
    padded = deepcopy(m)
    data = padded["payload"]["body"]["data"]
    padded["payload"]["body"]["data"] = data + "=" * (-len(data) % 4)
    other = source(Fake(*full_steps(padded))).sync(since=LOWER, cursor=None)
    assert other.value.messages == result.value.messages


def test_repeated_transport_headers_are_not_evidence():
    m = message()
    m["payload"]["headers"].extend([dict(name="Received", value="synthetic hop one"), dict(name="Received", value="synthetic hop two")])
    assert source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None).error is None


@pytest.mark.parametrize("subject", ["=?utf-8?B?!!!!?=", "=?utf-8?Q?bad=ZX?=", "=?utf-8?broken", "bad\nheader"])
def test_bad_header_encoding_is_not_repaired(subject):
    m = message()
    for h in m["payload"]["headers"]:
        if h["name"] == "Subject":
            h["value"] = subject
    assert_error(source(Fake(*full_steps(m))).sync(since=LOWER, cursor=None), Code.INVALID_RESPONSE)


def test_history_cycles_decreasing_checkpoints_and_page_bound():
    s = source(Fake(("history", dict(historyId="20", nextPageToken="p")), ("history", dict(historyId="19"))))
    assert_error(s.sync(since=LOWER, cursor=s._cursor(LOWER, "10")), Code.INVALID_RESPONSE)
    s = source(Fake(("history", dict(historyId="20", nextPageToken="p")), ("history", dict(historyId="20", nextPageToken="p"))))
    assert_error(s.sync(since=LOWER, cursor=s._cursor(LOWER, "10")), Code.INVALID_RESPONSE)
    s = source(Fake(("history", dict(historyId="20", nextPageToken="p"))), limits=GmailLimits(max_history_pages=1))
    assert_error(s.sync(since=LOWER, cursor=s._cursor(LOWER, "10")), Code.UNSUPPORTED_CAPABILITY)


def test_bootstrap_expiration_and_absent_profile_never_invent_cursor():
    assert_error(source(Fake(("profile", {}))).sync(since=LOWER, cursor=None), Code.INVALID_RESPONSE)
    assert_error(source(Fake(("profile", dict(historyId="10")), ("messages", {}),
                             ("history", GmailFailure(Code.CURSOR_EXPIRED)))).sync(since=LOWER, cursor=None), Code.CURSOR_EXPIRED)


def test_retained_membership_stale_commit_is_rejected(tmp_path):
    repo = BookingRepository(tmp_path / "stale.sqlite3", as_of=NOW)
    try:
        service = MailSynchronization(repo, authorized_travelers=TRAVELERS)
        assert service.run(source(Fake(*full_steps())), "GMAIL", "account-1", as_of=NOW).success
        def concurrent_lookup(**identity):
            # A second successful run changes expected state while source is reading.
            assert service.run(source(Fake(*full_steps(checkpoint="30"))), "GMAIL", "account-1", as_of=NOW + timedelta(minutes=1), full=True).success
            return True
        client = Fake(*delta_steps(event(), messages=[message(when=LOWER - timedelta(days=1))]))
        result = service.run(source(client, concurrent_lookup), "GMAIL", "account-1", as_of=NOW + timedelta(minutes=2))
        assert not result.success
        assert json.loads(repo.sync_state("GMAIL", "account-1")["cursor"])["checkpoint"] == "30"
        assert not repo.has_retained_mail_identity(provider="GMAIL", account_id="account-1", message_id="m1")
    finally:
        repo.close()
