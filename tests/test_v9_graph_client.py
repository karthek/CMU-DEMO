"""Fabricated protocol bytes only; no tokens, accounts or network services."""
from dataclasses import replace
from io import BytesIO
import json

import pytest

from travel_agent.live.graph_client import (GraphAccountBinding, GraphAccessSession,
    GraphFailure, GraphLimits, MicrosoftGraphHttpClient, MAIL_READ, route_url, validate_url)
from travel_agent.live.providers import ProviderErrorCode as Code


BINDING = GraphAccountBinding("00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002", "00000000-0000-0000-0000-000000000002")
SESSION = GraphAccessSession(BINDING, frozenset({MAIL_READ}), "offline-placeholder")
ROUTE = ("mailFolders", "root", "messages", "delta")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


class Credentials:
    def __init__(self, session=SESSION):
        self.session, self.calls = session, 0

    def verified_session(self):
        self.calls += 1
        if isinstance(self.session, Exception):
            raise self.session
        return self.session


class Response:
    def __init__(self, value, status=200, headers=None):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.stream, self.status = BytesIO(self.raw), status
        self.headers, self.reads, self.closed = headers or {}, [], False

    def read(self, maximum, *, deadline):
        self.reads.append((maximum, deadline))
        return self.stream.read(maximum)

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def open(self, url, *, headers, deadline):
        self.calls.append((url, headers, deadline))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport, credentials=None, clock=lambda: 0):
    return MicrosoftGraphHttpClient(binding=BINDING, credentials=credentials or Credentials(),
                                    transport=transport, monotonic=clock)


def error(call, code, retryable=None):
    with pytest.raises(GraphFailure) as caught:
        call()
    assert caught.value.error.code == code
    if retryable is not None:
        assert caught.value.error.retryable == retryable
    return caught.value.error


def test_immutable_preference_and_full_body_preference():
    transport = Transport(Response({"ok": 1}))
    attempt = client(transport).begin(GraphLimits())
    attempt.read(("messages", "A/B+=Case"), body=True)
    url, headers, deadline = transport.calls[0]
    assert "A%2FB%2B%3DCase" in url
    assert 'IdType="ImmutableId"' in headers["Prefer"]
    assert 'outlook.body-content-type="html"' in headers["Prefer"]
    assert headers["Accept-Encoding"] == "identity"
    assert deadline == 20


@pytest.mark.parametrize("raw", [b'{"x":1}       ', b'{"x":"\\u0061"}', '{"x":"é"}'.encode()])
def test_raw_boundary_inclusive_and_not_semantic_size(raw):
    response = Response(raw)
    attempt = client(Transport(response)).begin(GraphLimits(response_bytes=len(raw), attempt_bytes=len(raw)))
    assert attempt.read(ROUTE)["x"] in (1, "a", "é")
    assert attempt.bytes == len(raw)
    assert response.reads[-1][0] == 1  # bounded EOF confirmation


@pytest.mark.parametrize("field", ["response_bytes", "attempt_bytes"])
@pytest.mark.parametrize("raw", [b'{"x":1}       ', b'{"x":"\\u0061"}', b' { "x" : 1 } \n'])
def test_overflow_rejected_before_json_decode(monkeypatch, field, raw):
    called = []
    monkeypatch.setattr("travel_agent.live.graph_client.decode_json", lambda *a: called.append(a))
    response = Response(raw + b"unused")
    attempt = client(Transport(response)).begin(replace(GraphLimits(), **{field: len(raw) - 1}))
    error(lambda: attempt.read(ROUTE), Code.INVALID_RESPONSE)
    assert response.stream.tell() == len(raw)
    assert attempt.bytes == len(raw)  # rejected probe counted, rest unread
    assert not called and response.closed


@pytest.mark.parametrize("over", [False, True])
def test_multiple_responses_accumulate_across_attempt(over):
    responses = [Response(b'{"x":1}'), Response(b'{"x":2}' + (b" " if over else b""))]
    attempt = client(Transport(*responses)).begin(GraphLimits(attempt_bytes=14))
    attempt.read(ROUTE)
    if over:
        error(lambda: attempt.read(ROUTE), Code.INVALID_RESPONSE)
        assert attempt.bytes == 15
    else:
        assert attempt.read(ROUTE) == {"x": 2}
        assert attempt.bytes == 14


def test_failed_and_completed_attempts_reset_budgets():
    c = client(Transport(Response(b'{"x":1} '), Response(b'{"x":1}'), Response(b'{"x":1}')))
    limits = GraphLimits(attempt_bytes=7)
    error(lambda: c.begin(limits).read(ROUTE), Code.INVALID_RESPONSE)
    for _ in range(2):
        attempt = c.begin(limits)
        assert attempt.bytes == 0
        attempt.read(ROUTE)
        assert attempt.bytes == 7


@pytest.mark.parametrize("status,code,retry", [
    (400, Code.INVALID_RESPONSE, False), (401, Code.AUTH_REQUIRED, False),
    (403, Code.PERMISSION_DENIED, False), (404, Code.NOT_FOUND, False),
    (409, Code.UNAVAILABLE, True), (410, Code.NOT_FOUND, False),
    (412, Code.UNAVAILABLE, True), (429, Code.RATE_LIMITED, True),
    (500, Code.UNAVAILABLE, True), (501, Code.UNSUPPORTED_CAPABILITY, False),
    (502, Code.UNAVAILABLE, True), (503, Code.UNAVAILABLE, True),
    (504, Code.TIMEOUT, True), (509, Code.RATE_LIMITED, True),
    (302, Code.INVALID_RESPONSE, False),
])
def test_status_mapping_and_zero_retries(status, code, retry):
    response = Response({"error": {"code": "unknown", "message": "DO NOT EXPOSE"}}, status)
    transport = Transport(response)
    result = error(lambda: client(transport).begin(GraphLimits()).read(ROUTE), code, retry)
    assert len(transport.calls) == 1 and response.closed
    assert "DO NOT EXPOSE" not in repr(result)


@pytest.mark.parametrize("status,payload,established", [
    (410, {}, False), (400, {"error": {"innerError": {"code": "syncStateNotFound"}}}, True),
    (404, {}, True)])
def test_delta_expiration(status, payload, established):
    attempt = client(Transport(Response(payload, status))).begin(GraphLimits())
    error(lambda: attempt.read(ROUTE, delta=True, established=established), Code.CURSOR_EXPIRED)


def test_claims_challenge():
    attempt = client(Transport(Response({"error": {"code": "insufficient_claims"}}, 403))).begin(GraphLimits())
    error(lambda: attempt.read(ROUTE), Code.AUTH_REQUIRED)


@pytest.mark.parametrize("header,expected", [("12", 12), ("0", 0), ("-1", None), ("soon", None), (None, None)])
def test_retry_after(header, expected):
    attempt = client(Transport(Response({}, 429, {"Retry-After": header} if header is not None else {}))).begin(GraphLimits())
    result = error(lambda: attempt.read(ROUTE), Code.RATE_LIMITED)
    assert result.retry_after_seconds == expected


@pytest.mark.parametrize("raw", [b"bad", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":"\xff"}', b"[]", b""])
def test_invalid_json(raw):
    error(lambda: client(Transport(Response(raw))).begin(GraphLimits()).read(ROUTE), Code.INVALID_RESPONSE)


def test_json_depth_and_encoding():
    error(lambda: client(Transport(Response({"a": {"b": 1}}))).begin(GraphLimits(json_depth=1)).read(ROUTE), Code.INVALID_RESPONSE)
    error(lambda: client(Transport(Response({}, headers={"Content-Encoding": "gzip"}))).begin(GraphLimits()).read(ROUTE), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("exception,code", [(TimeoutError("private"), Code.TIMEOUT),
    (OSError("private"), Code.UNAVAILABLE), (ValueError("private"), Code.INVALID_RESPONSE)])
def test_transport_exceptions_do_not_escape(exception, code):
    error(lambda: client(Transport(exception)).begin(GraphLimits()).read(ROUTE), code)


@pytest.mark.parametrize("stage", ["connect", "read", "between_requests"])
def test_absolute_deadline_is_not_reset(stage):
    now = [0]
    response = Response({"ok": True})
    class Delayed(Transport):
        def open(self, *args, **kwargs):
            result = super().open(*args, **kwargs)
            if stage == "connect":
                now[0] = kwargs["deadline"]
            return result
    original_read = response.read
    def read(maximum, *, deadline):
        chunk = original_read(maximum, deadline=deadline)
        now[0] = deadline
        return chunk
    if stage == "read":
        response.read = read
    transport = Delayed(response)
    attempt = client(transport, clock=lambda: now[0]).begin(GraphLimits(attempt_seconds=3))
    if stage == "between_requests":
        now[0] = 3
    error(lambda: attempt.read(ROUTE), Code.TIMEOUT, True)
    if transport.calls:
        assert transport.calls[0][2] == 3


@pytest.mark.parametrize("session,code", [
    (None, Code.AUTH_REQUIRED), (RuntimeError("sensitive"), Code.AUTH_REQUIRED),
    (replace(SESSION, access_token="bad\r\nheader"), Code.AUTH_REQUIRED),
    (replace(SESSION, delegated_scopes=frozenset()), Code.PERMISSION_DENIED),
    (replace(SESSION, delegated_scopes=frozenset({MAIL_READ, "Mail.ReadWrite"})), Code.PERMISSION_DENIED),
    (replace(SESSION, binding=replace(BINDING, tenant_id="00000000-0000-0000-0000-000000000003")), Code.AUTH_REQUIRED),
])
def test_account_session_binding_fails_before_mail(session, code):
    transport = Transport()
    error(lambda: client(transport, Credentials(session)).begin(GraphLimits()), code)
    assert not transport.calls


def test_session_is_revalidated_and_secret_repr_hidden():
    creds = Credentials()
    c = client(Transport(), creds)
    c.begin(GraphLimits())
    c.begin(GraphLimits())
    assert creds.calls == 2 and SESSION.access_token not in repr(SESSION)
    with pytest.raises(ValueError):
        replace(BINDING, mailbox_user_id="00000000-0000-0000-0000-000000000004")


@pytest.mark.parametrize("change", [
    lambda u: u.replace("https:", "http:"), lambda u: u.replace("graph.microsoft.com", "evil.example.test"),
    lambda u: u + "#fragment", lambda u: u.replace("/v1.0/", "/beta/"),
    lambda u: u.replace("/root/", "/wrong/"), lambda u: u.replace("https://", "https://user@"),
    lambda u: u + "\n", lambda u: u.replace("/root/", "/%2e%2e/"),
    lambda u: u + "?broken=%xy", lambda u: u.replace("/root/", "/%2572oot/"),
])
def test_unsafe_routes_rejected_before_credentials_leave(change):
    transport = Transport()
    attempt = client(transport).begin(GraphLimits())
    error(lambda: attempt.read(ROUTE, url=change(route_url(BINDING, ROUTE))), Code.INVALID_RESPONSE)
    assert not transport.calls


def test_opaque_query_and_odata_predicate_are_preserved():
    url = route_url(BINDING, ROUTE).replace("users/" + BINDING.principal_id,
        "users('" + BINDING.principal_id + "')").replace("mailFolders/root", "mailFolders('root')")
    url += "?$deltatoken=opaque%2B%2F%3D"
    transport = Transport(Response({"ok": 1}))
    client(transport).begin(GraphLimits()).read(ROUTE, url=url, delta=True)
    assert transport.calls[0][0] == url


def test_error_bytes_share_budget_and_overflow_precedes_status():
    attempt = client(Transport(Response(b'{"ok":1}'), Response(b'{"error":{}}', 429))).begin(GraphLimits(attempt_bytes=10))
    attempt.read(ROUTE)
    error(lambda: attempt.read(ROUTE), Code.INVALID_RESPONSE)
    assert attempt.bytes == 11


def test_http_header_names_are_case_insensitive():
    attempt = client(Transport(Response({}, 429, {"retry-after": "12"}))).begin(GraphLimits())
    assert error(lambda: attempt.read(ROUTE), Code.RATE_LIMITED).retry_after_seconds == 12
    attempt = client(Transport(Response({}, headers={"content-encoding": "gzip"}))).begin(GraphLimits())
    error(lambda: attempt.read(ROUTE), Code.INVALID_RESPONSE)


def test_non_mail_read_route_is_not_an_added_capability():
    transport = Transport()
    attempt = client(transport).begin(GraphLimits())
    error(lambda: attempt.read(("calendar", "events")), Code.UNSUPPORTED_CAPABILITY)
    assert not transport.calls


def test_nonconforming_transport_chunk_is_rejected():
    response = Response({})
    response.read = lambda maximum, deadline: b" " * (maximum + 1)
    attempt = client(Transport(response)).begin(GraphLimits())
    error(lambda: attempt.read(ROUTE), Code.INVALID_RESPONSE)
    assert response.closed
