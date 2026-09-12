"""Stub the HTTP opener; exercise real request building and safe error mapping."""
from io import BytesIO
from datetime import timedelta
import json
import ssl
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

from test_v9_gmail import no_network, source, LOWER, message, leaf, full_steps, NOW, TRAVELERS
from travel_agent.live.gmail import GmailLimits
from travel_agent.live.gmail_client import GmailHttpClient, GmailFailure, GmailByteBudget, GMAIL_READONLY_SCOPE, _NoRedirect
from travel_agent.live.booking_repository import BookingRepository
from travel_agent.live.mail_sync import MailSynchronization
from travel_agent.live.providers import ProviderErrorCode as Code


def http_client(outcome, **kwargs):
    # Deliberately fake value: this test never sends a request.
    client = GmailHttpClient(lambda: "synthetic-test-only", **kwargs)
    client._opener = Mock()
    if isinstance(outcome, Exception):
        client._opener.open.side_effect = outcome
    else:
        client._opener.open.return_value = BytesIO(outcome)
    return client


@pytest.mark.parametrize("status,reason,code,retry", [
    (401, "authError", Code.AUTH_REQUIRED, False),
    (403, "forbidden", Code.PERMISSION_DENIED, False),
    (403, "domainPolicy", Code.PERMISSION_DENIED, False),
    (403, "rateLimitExceeded", Code.RATE_LIMITED, True),
    (403, "userRateLimitExceeded", Code.RATE_LIMITED, True),
    (403, "dailyLimitExceeded", Code.RATE_LIMITED, False),
    (429, "", Code.RATE_LIMITED, True),
    (500, "", Code.UNAVAILABLE, True),
    (502, "", Code.UNAVAILABLE, True),
    (503, "", Code.UNAVAILABLE, True),
    (504, "", Code.UNAVAILABLE, True),
    (400, "", Code.INVALID_RESPONSE, False),
    (302, "", Code.INVALID_RESPONSE, False),
])
def test_status_mapping_and_no_error_text_leak(status, reason, code, retry):
    payload = json.dumps(dict(error=dict(message="private-response-content", errors=[dict(reason=reason)]))).encode()
    error = HTTPError("https://example.test/private", status, "private-error", {"Retry-After": "120"}, BytesIO(payload))
    client = http_client(error)
    result = source(client).sync(since=LOWER, cursor=None)
    assert result.error.code == code
    assert result.error.retryable is retry
    assert "private" not in repr(result)
    if code in (Code.RATE_LIMITED, Code.UNAVAILABLE):
        assert result.error.retry_after_seconds == 120


@pytest.mark.parametrize("resource,code", [("history", Code.CURSOR_EXPIRED), ("messages/m1", Code.NOT_FOUND), ("messages/m1/attachments/a1", Code.NOT_FOUND)])
def test_404_context(resource, code):
    client = http_client(HTTPError("https://example.test", 404, "missing", {}, BytesIO(b"{}")))
    with pytest.raises(GmailFailure) as failure:
        client.read(resource, params={}, timeout=1)
    assert failure.value.error.code == code


@pytest.mark.parametrize("error,code,retry", [
    (TimeoutError("private"), Code.TIMEOUT, True),
    (URLError(TimeoutError("private")), Code.TIMEOUT, True),
    (URLError("DNS interrupted"), Code.UNAVAILABLE, True),
    (URLError(ssl.SSLError("certificate")), Code.UNAVAILABLE, False),
    (ssl.SSLError("certificate"), Code.UNAVAILABLE, False),
    (ConnectionError("private"), Code.UNAVAILABLE, True),
    (RuntimeError("private"), Code.INVALID_RESPONSE, False),
])
def test_transport_mapping(error, code, retry):
    result = source(http_client(error)).sync(since=LOWER, cursor=None)
    assert result.error.code == code
    assert result.error.retryable is retry
    assert "private" not in repr(result)


@pytest.mark.parametrize("body,code", [(b"not-json", Code.INVALID_RESPONSE), (b"[]", Code.INVALID_RESPONSE), (b"null", Code.INVALID_RESPONSE), (b"x" * 21, Code.UNSUPPORTED_CAPABILITY)])
def test_malformed_and_bounded_response(body, code):
    result = source(http_client(body, max_response_bytes=20)).sync(since=LOWER, cursor=None)
    assert result.error.code == code


def test_readonly_get_url_timeout_and_redirect_policy():
    client = http_client(b'{"messages":[]}')
    assert client.read("messages", params={"q": "after:123 -in:drafts", "includeSpamTrash": "true"}, timeout=2) == {"messages": []}
    call = client._opener.open.call_args
    request = call.args[0]
    assert request.get_method() == "GET"
    assert request.full_url == "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=after%3A123+-in%3Adrafts&includeSpamTrash=true"
    assert call.kwargs == {"timeout": 2}
    assert GMAIL_READONLY_SCOPE.endswith("/gmail.readonly")
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test") is None
    with pytest.raises(GmailFailure):
        client.read("https://example.test", params={}, timeout=2)


def test_runtime_credential_failure_is_safe_and_performs_no_http():
    client = http_client(b"{}")
    client._token = Mock(side_effect=RuntimeError("private"))
    result = source(client).sync(since=LOWER, cursor=None)
    assert result.error.code == Code.AUTH_REQUIRED
    client._opener.open.assert_not_called()


@pytest.mark.parametrize("payload", [b'{"historyId":"1","historyId":"2"}', b'{"number":NaN}'])
def test_ambiguous_json_is_rejected(payload):
    result = source(http_client(payload)).sync(since=LOWER, cursor=None)
    assert result.error.code == Code.INVALID_RESPONSE


class ObservedBody(BytesIO):
    """Record consumed body bytes even after the HTTP context closes the stream."""
    def __init__(self, payload):
        super().__init__(payload)
        self.consumed = 0
        self.read_sizes = []

    def read1(self, size):
        self.read_sizes.append(size)
        chunk = super().read1(size)
        self.consumed += len(chunk)
        return chunk


def wire_client(payloads):
    client = http_client(b"")
    streams = [ObservedBody(payload) for payload in payloads]
    client._opener.open.side_effect = streams
    return client, streams


def empty_wire():
    return [b'{"historyId":"10"}', b'{"messages":[]}',
            b'{"historyId":"10"}', b'{"historyId":"10"}']


@pytest.mark.parametrize("payload", [
    b'{"historyId":"10"}' + b' ' * 100,
    b'{"historyId":"\\u0031\\u0030"}',
])
def test_received_bytes_overflow_precedes_json_parse_and_cursor(payload, monkeypatch):
    assert len(json.dumps(json.loads(payload)).encode()) < 20 < len(payload)
    client, streams = wire_client([payload])
    decoder = Mock(side_effect=AssertionError("Oversized body must not be parsed"))
    monkeypatch.setattr("travel_agent.live.gmail_client.json.loads", decoder)
    result = source(client, limits=GmailLimits(max_response_bytes=20)).sync(since=LOWER, cursor=None)
    assert result.value is None
    assert result.error.code == Code.UNSUPPORTED_CAPABILITY
    assert result.error.retryable is False
    decoder.assert_not_called()
    assert streams[0].consumed == 21  # One rejected overflow-probe byte.
    assert streams[0].closed
    assert client._opener.open.call_count == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_received_bytes_inclusive_response_boundary(extra):
    payload = b'{"historyId":"10"}'
    client, streams = wire_client([payload + b' ' * extra])
    budget = GmailByteBudget(len(payload), 100)
    if extra:
        with pytest.raises(GmailFailure) as failure:
            client.read("profile", params={}, timeout=2, budget=budget)
        assert failure.value.error.code == Code.UNSUPPORTED_CAPABILITY
    else:
        assert client.read("profile", params={}, timeout=2, budget=budget) == {"historyId": "10"}
    assert budget.received_bytes == streams[0].consumed == len(payload) + extra


@pytest.mark.parametrize("flow", ["pagination", "attachment"])
@pytest.mark.parametrize("shortfall", [0, 1])
def test_received_bytes_accumulate_across_all_attempt_responses(flow, shortfall):
    if flow == "pagination":
        responses = [dict(historyId="10"), dict(messages=[], nextPageToken="next"),
                     dict(messages=[]), dict(historyId="10"), dict(historyId="10")]
    else:
        payload = leaf()
        attachment = payload["body"]
        payload["body"] = dict(attachmentId="body1", size=attachment["size"])
        responses = [body for _, body in full_steps(message(payload=payload))]
        responses.insert(3, attachment)
    # Deliberately format each response differently from the default json.dumps.
    payloads = [json.dumps(body, indent=2).encode() + b' \n' for body in responses]
    total = sum(map(len, payloads))
    client, streams = wire_client(payloads)
    result = source(client, limits=GmailLimits(max_response_bytes=max(map(len, payloads)),
                    max_batch_bytes=total - shortfall)).sync(since=LOWER, cursor=None)
    if shortfall:
        assert result.value is None
        assert result.error.code == Code.UNSUPPORTED_CAPABILITY
    else:
        assert result.error is None
        assert result.value.completed_cursor
        assert len(result.value.messages) == (flow == "attachment")
    assert sum(stream.consumed for stream in streams) == total
    assert all(stream.closed for stream in streams)
    assert client._opener.open.call_count == len(payloads)


def test_received_bytes_reproduction_stops_at_attempt_budget_and_resets():
    oversized = [body.ljust(90, b' ') for body in empty_wire()[:2]]
    client, streams = wire_client(oversized + empty_wire() + empty_wire())
    s = source(client, limits=GmailLimits(max_response_bytes=100, max_batch_bytes=100))
    failed = s.sync(since=LOWER, cursor=None)
    assert failed.value is None
    assert failed.error.code == Code.UNSUPPORTED_CAPABILITY
    assert [stream.consumed for stream in streams[:2]] == [90, 11]
    assert client._opener.open.call_count == 2
    # The same client and source work after both failed and successful attempts.
    first = s.sync(since=LOWER, cursor=None)
    second = s.sync(since=LOWER, cursor=None)
    assert first.error is second.error is None
    assert first.value == second.value
    assert client._opener.open.call_count == 10


@pytest.mark.parametrize("payload", [
    b'{"x":"\\u00e9"}',
    b'{"x":"\xc3\xa9"}',
    b'{ "x" : "\\u00E9" }\r\n',
    b'\n\t{"x":"\xc3\xa9"}    ',
])
def test_received_bytes_unicode_and_formatting_use_actual_wire_length(payload):
    client, streams = wire_client([payload, payload])
    exact = GmailByteBudget(len(payload), len(payload))
    assert client.read("profile", params={}, timeout=2, budget=exact) == {"x": "\u00e9"}
    assert exact.received_bytes == len(payload)
    short = GmailByteBudget(len(payload), len(payload) - 1)
    with pytest.raises(GmailFailure) as failure:
        client.read("profile", params={}, timeout=2, budget=short)
    assert failure.value.error.code == Code.UNSUPPORTED_CAPABILITY
    assert short.received_bytes == streams[1].consumed == len(payload)


def test_received_bytes_stop_streaming_after_one_overflow_probe():
    client, streams = wire_client([b' ' * 200_000])
    budget = GmailByteBudget(100_000, 65_540)
    with pytest.raises(GmailFailure) as failure:
        client.read("profile", params={}, timeout=2, budget=budget)
    assert failure.value.error.code == Code.UNSUPPORTED_CAPABILITY
    assert streams[0].read_sizes == [65_536, 5]
    assert streams[0].consumed == budget.received_bytes == 65_541
    assert streams[0].closed


def test_received_bytes_http_error_body_shares_attempt_budget():
    client, streams = wire_client([empty_wire()[0].ljust(90, b' ')])
    error_body = ObservedBody(b'{"error":{}}'.ljust(90, b' '))
    error = HTTPError("https://example.test/private", 429, "private", {}, error_body)
    client._opener.open.side_effect = [streams[0], error]
    result = source(client, limits=GmailLimits(max_response_bytes=100, max_batch_bytes=100)).sync(since=LOWER, cursor=None)
    assert result.value is None
    assert result.error.code == Code.UNSUPPORTED_CAPABILITY
    assert result.error.retryable is False
    assert "private" not in repr(result)
    assert streams[0].consumed + error_body.consumed == 101
    assert error_body.closed


def test_received_bytes_error_diagnostic_cap_keeps_status_mapping():
    body = ObservedBody(b'{"error":{}}'.ljust(100_000, b' '))
    client = http_client(HTTPError("https://example.test", 429, "limit", {}, body))
    budget = GmailByteBudget(200_000, 200_000)
    with pytest.raises(GmailFailure) as failure:
        client.read("history", params={}, timeout=2, budget=budget)
    assert failure.value.error.code == Code.RATE_LIMITED
    assert budget.received_bytes == body.consumed == 65_537
    assert body.closed


@pytest.mark.parametrize("ticks,consumed", [([0, 3], 0), ([0, 0, 3], 2)])
@pytest.mark.parametrize("http_error", [False, True])
def test_received_bytes_preserve_deadlines(ticks, consumed, http_error, monkeypatch):
    client, streams = wire_client([b'{}'])
    if http_error:
        client._opener.open.side_effect = HTTPError("https://example.test", 429, "limit", {}, streams[0])
    monkeypatch.setattr("travel_agent.live.gmail_client.time.monotonic", iter(ticks).__next__)
    budget = GmailByteBudget(10, 10)
    with pytest.raises(GmailFailure) as failure:
        client.read("profile", params={}, timeout=2, budget=budget)
    assert failure.value.error.code == Code.TIMEOUT
    assert failure.value.error.retryable is True
    assert budget.received_bytes == streams[0].consumed == consumed
    assert streams[0].closed


def test_received_bytes_standalone_reads_have_independent_budgets():
    client, streams = wire_client([b'{}', b'{}'])
    client._limit = 2
    assert client.read("profile", params={}, timeout=2) == {}
    assert client.read("profile", params={}, timeout=2) == {}
    assert [stream.consumed for stream in streams] == [2, 2]


def test_received_bytes_exhaustion_cannot_commit_cursor_or_evidence(tmp_path):
    payloads = empty_wire() + [b'{"historyId":"20"}'.ljust(90, b' ')] * 2
    client, _ = wire_client(payloads)
    s = source(client, limits=GmailLimits(max_response_bytes=100, max_batch_bytes=100))
    repo = BookingRepository(tmp_path / "budget.sqlite3", as_of=NOW)
    try:
        service = MailSynchronization(repo, authorized_travelers=TRAVELERS)
        initial = service.run(s, "GMAIL", "account-1", as_of=NOW)
        assert initial.success
        failed = service.run(s, "GMAIL", "account-1", as_of=NOW + timedelta(minutes=1))
        assert not failed.success
        assert failed.error.code == Code.UNSUPPORTED_CAPABILITY
        assert failed.cursor == initial.cursor == repo.sync_state("GMAIL", "account-1")["cursor"]
        assert not repo.resync_required("GMAIL", "account-1")
        assert repo.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
        assert repo.connection.execute("SELECT COUNT(*) FROM mail_sync_runs").fetchone()[0] == 1
    finally:
        repo.close()
