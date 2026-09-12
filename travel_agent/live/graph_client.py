"""Read-only Graph boundary; runtime authentication and transport are injected.

The credential port must validate the authentication session, not decode an opaque
access token. Neither tokens nor provider diagnostics are returned to the core.
"""
from dataclasses import dataclass, field
import json
import re
import time
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from travel_agent.live.providers import ProviderError, ProviderErrorCode as Code


ORIGIN = "https://graph.microsoft.com"
MAIL_READ = ORIGIN + "/Mail.Read"


class GraphFailure(Exception):
    def __init__(self, code=Code.INVALID_RESPONSE, retryable=False, retry_after=None):
        self.error = ProviderError(code, retryable, retry_after)
        super().__init__(code.value)


def require(condition, code=Code.INVALID_RESPONSE):
    if not condition:
        raise GraphFailure(code)


def identifier(value):
    require(isinstance(value, str) and bool(value) and len(value) <= 4096
            and not any(ord(c) < 33 or ord(c) == 127 for c in value))
    return value


@dataclass(frozen=True)
class GraphAccountBinding:
    tenant_id: str
    principal_id: str
    mailbox_user_id: str

    def __post_init__(self):
        for value in (self.tenant_id, self.principal_id, self.mailbox_user_id):
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("Canonical GUID binding required")
        if self.mailbox_user_id != self.principal_id:
            raise ValueError("Own primary mailbox binding required")

    @property
    def account_id(self):
        return f"graph-global:{self.tenant_id}:{self.principal_id}"


@dataclass(frozen=True)
class GraphAccessSession:
    """Result of the trusted runtime's validation, never caller identity labels.

    The runtime port validates issuer/audience/expiry/nonce and binds these claims
    and delegated scopes to this token from the SAME authentication session.
    This value object is not itself an identity-token verifier.
    """
    binding: GraphAccountBinding
    delegated_scopes: frozenset[str]
    access_token: str = field(repr=False)


class GraphCredentialProvider(Protocol):
    def verified_session(self) -> GraphAccessSession:
        """Return validated own-mailbox credentials or raise; no implicit refresh."""
        ...


class GraphResponse(Protocol):
    status: int
    headers: object

    def read(self, maximum: int, *, deadline: float) -> bytes:
        """Read at most maximum bytes by the absolute monotonic deadline."""
        ...

    def close(self) -> None:
        """Release/cancel resources without blocking on more provider data."""
        ...


class GraphTransport(Protocol):
    def open(self, url: str, *, headers: dict, deadline: float) -> GraphResponse:
        """GET without redirects/retries/decompression, bounded connect AND headers.

        The runtime transport must enforce this absolute deadline during blocking
        I/O (including name resolution), using the client's monotonic clock.
        A socket inactivity timeout alone does not satisfy this port.
        """
        ...


@dataclass(frozen=True)
class GraphLimits:
    response_bytes: int = 4 * 1024 * 1024
    attempt_bytes: int = 64 * 1024 * 1024
    requests: int = 500
    pages: int = 400
    delta_entries: int = 20000
    candidate_ids: int = 10000
    emitted_items: int = 10000
    folders: int = 100
    enumerated_folders: int = 200
    folder_depth: int = 20
    body_bytes: int = 1024 * 1024
    attachments: int = 32
    recipients: int = 500
    headers: int = 200
    header_bytes: int = 64 * 1024
    json_depth: int = 32
    link_bytes: int = 16 * 1024
    cursor_bytes: int = 256 * 1024
    page_size: int = 100
    attempt_seconds: int = 120
    request_seconds: int = 20

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in vars(self).values()):
            raise ValueError("Positive integer Graph bounds required")
        if self.emitted_items > 10000:
            raise ValueError("Core synchronization item bound exceeded")


def decode_json(raw, max_depth):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: require(False))
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            require(depth <= max_depth)
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        require(isinstance(value, dict))
        return value
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise GraphFailure() from None


def route_url(binding, route, query=""):
    parts = ("users", binding.mailbox_user_id, *route)
    return ORIGIN + "/v1.0/" + "/".join(quote(p, safe="") for p in parts) + query


def validate_url(url, binding, route, maximum):
    """Validate routing only; never interpret/rebuild opaque continuation queries."""
    allowed = (route == ("mailFolders", "msgfolderroot")
        or (len(route) == 3 and route[0] == "mailFolders" and route[2] == "childFolders")
        or (len(route) == 4 and route[0] == "mailFolders" and route[2:] == ("messages", "delta"))
        or (len(route) == 2 and route[0] == "messages")
        or (len(route) == 3 and route[0] == "messages" and route[2] == "attachments"))
    require(allowed, Code.UNSUPPORTED_CAPABILITY)
    require(isinstance(url, str) and len(url.encode("utf-8")) <= maximum)
    require(not any(ord(c) < 33 or ord(c) == 127 for c in url) and "\\" not in url)
    require(not re.search(r"%(?![a-fA-F0-9]{2})", url))
    try:
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com"
                and parsed.netloc in ("graph.microsoft.com", "graph.microsoft.com:443")
                and not parsed.fragment and not parsed.username and not parsed.password)
        segments = parsed.path.split("/")[1:]
        expanded = []
        for segment in segments:
            segment = unquote(segment, errors="strict")
            require(segment not in ("", ".", "..") and "%" not in segment
                    and not any(ord(c) < 32 or ord(c) == 127 for c in segment))
            match = re.fullmatch(r"(users|mailFolders|messages)\('((?:[^']|'')+)'\)", segment)
            if match:
                expanded.extend((match[1], match[2].replace("''", "'")))
            else:
                expanded.append(segment)
        require(tuple(expanded) == ("v1.0", "users", binding.mailbox_user_id, *route))
        return url
    except (ValueError, UnicodeError):
        raise GraphFailure() from None


class GraphAttempt:
    """No attempt counters live on the reusable client or source."""
    def __init__(self, client, limits, session):
        self.client, self.limits, self.session = client, limits, session
        self.clock = client.clock
        self.deadline = self.clock() + limits.attempt_seconds
        self.bytes = self.requests = self.pages = self.entries = 0
        self.enumerated_folders = 0

    def check(self, deadline=None):
        if self.clock() >= min(self.deadline, deadline or self.deadline):
            raise GraphFailure(Code.TIMEOUT, True)

    def read(self, route, *, url=None, query="", delta=False, established=False, body=False):
        return self.client.read(self, route, url=url, query=query, delta=delta,
                                established=established, body=body)


class MicrosoftGraphHttpClient:
    """HTTP protocol client with mandatory deadline-capable injected transport.

    No default urllib transport: its inactivity timeouts do not bound DNS/header
    slow-drip duration. Runtime transport implementation is an explicit capability.
    """
    def __init__(self, *, binding: GraphAccountBinding, credentials: GraphCredentialProvider,
                 transport: GraphTransport, monotonic=time.monotonic):
        if not isinstance(binding, GraphAccountBinding):
            raise ValueError("Bound Graph account required")
        self.binding, self.credentials, self.transport = binding, credentials, transport
        self.clock = monotonic

    def begin(self, limits):
        started = self.clock()
        try:
            session = self.credentials.verified_session()
            require(isinstance(session, GraphAccessSession) and session.binding == self.binding,
                    Code.AUTH_REQUIRED)
            require(isinstance(session.delegated_scopes, frozenset)
                    and session.delegated_scopes == frozenset({MAIL_READ}), Code.PERMISSION_DENIED)
            token = session.access_token
            require(isinstance(token, str) and bool(token) and all(33 <= ord(c) < 127 for c in token),
                    Code.AUTH_REQUIRED)
        except GraphFailure:
            raise
        except Exception:
            raise GraphFailure(Code.AUTH_REQUIRED) from None
        attempt = GraphAttempt(self, limits, session)
        attempt.deadline = started + limits.attempt_seconds
        attempt.check()
        return attempt

    def read(self, attempt, route, *, url=None, query="", delta=False, established=False, body=False):
        response = None
        try:
            attempt.check()
            limit = attempt.limits
            url = validate_url(url if url is not None else route_url(self.binding, route, query),
                               self.binding, route, limit.link_bytes)
            attempt.requests += 1
            require(attempt.requests <= limit.requests)
            deadline = min(attempt.deadline, self.clock() + limit.request_seconds)
            preference = 'IdType="ImmutableId", odata.maxpagesize=' + str(limit.page_size)
            if body:
                preference += ', outlook.body-content-type="html"'
            response = self.transport.open(url, headers={
                "Authorization": "Bearer " + attempt.session.access_token,
                "Accept": "application/json", "Accept-Encoding": "identity", "Prefer": preference,
            }, deadline=deadline)
            attempt.check(deadline)
            headers = {}
            for name, value in response.headers.items():
                require(isinstance(name, str) and isinstance(value, str))
                name = name.lower()
                require(name not in headers or headers[name] == value)
                headers[name] = value
            require(headers.get("content-encoding", "identity").lower() == "identity")
            require(type(response.status) is int)
            raw = bytearray()
            while True:
                attempt.check(deadline)
                remaining = min(limit.response_bytes - len(raw), limit.attempt_bytes - attempt.bytes)
                maximum = min(65536, remaining + 1)
                chunk = response.read(maximum, deadline=deadline)
                require(isinstance(chunk, bytes))
                attempt.bytes += len(chunk)
                attempt.check(deadline)
                require(len(chunk) <= maximum and len(chunk) <= remaining)
                if not chunk:
                    break
                raw.extend(chunk)
            # HTTP failures can have an empty body. Malformed nonempty JSON is
            # still an invalid provider response; no raw diagnostics escape.
            result = decode_json(bytes(raw), limit.json_depth) if raw else {}
            attempt.check(deadline)
            if response.status != 200:
                self._error(response.status, result, headers, delta, established)
            require(raw and "error" not in result)
            attempt.check(deadline)
            return result
        except GraphFailure:
            raise
        except TimeoutError:
            raise GraphFailure(Code.TIMEOUT, True) from None
        except (OSError, ConnectionError):
            raise GraphFailure(Code.UNAVAILABLE, True) from None
        except Exception:
            raise GraphFailure() from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    @staticmethod
    def _error(status, payload, headers, delta, established):
        codes, part = set(), payload.get("error")
        while isinstance(part, dict):
            if isinstance(part.get("code"), str):
                codes.add(part["code"].lower())
            part = part.get("innerError", part.get("innererror"))
        delay = headers.get("retry-after")
        try:
            delay = int(delay) if isinstance(delay, str) and re.fullmatch(r"[0-9]+", delay) else None
        except ValueError:
            delay = None
        if delta and (status == 410 or (400 <= status < 500 and "syncstatenotfound" in codes)
                      or (established and status == 404)):
            raise GraphFailure(Code.CURSOR_EXPIRED)
        if status == 401 or (status == 403 and "insufficient_claims" in codes):
            raise GraphFailure(Code.AUTH_REQUIRED)
        if status == 403:
            raise GraphFailure(Code.PERMISSION_DENIED)
        if status in (404, 410):
            raise GraphFailure(Code.NOT_FOUND)
        if status in (429, 509):
            raise GraphFailure(Code.RATE_LIMITED, True, delay)
        if status == 504:
            raise GraphFailure(Code.TIMEOUT, True)
        if status == 501:
            raise GraphFailure(Code.UNSUPPORTED_CAPABILITY)
        if status in (409, 412) or 500 <= status <= 599:
            raise GraphFailure(Code.UNAVAILABLE, True, delay)
        raise GraphFailure()
