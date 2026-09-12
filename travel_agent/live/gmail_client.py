"""Small read-only Gmail REST boundary. No OAuth flow or credential persistence."""
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import socket
import ssl
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener

from travel_agent.live.providers import ProviderError, ProviderErrorCode as Code


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailFailure(Exception):
    """Safe internal failure, translated to ProviderResult at the source boundary."""
    def __init__(self, code, retryable=False, delay=None):
        self.error = ProviderError(code, retryable, delay)
        super().__init__(code.value)


@dataclass
class GmailByteBudget:
    """One attempt's received-body budget; never retained on a shared client.

    Limits are inclusive. One overflow-probe byte may be consumed to distinguish
    EOF at the limit; it is counted, rejected immediately and never JSON-decoded.
    """
    max_response_bytes: int
    max_batch_bytes: int
    received_bytes: int = field(default=0, init=False)

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.max_response_bytes, self.max_batch_bytes)):
            raise ValueError("Positive received-byte limits required")

    def read_body(self, response, *, response_limit, deadline, diagnostic_limit=None):
        limit = min(response_limit, self.max_response_bytes, self.max_batch_bytes - self.received_bytes)
        if limit < 0:
            raise GmailFailure(Code.UNSUPPORTED_CAPABILITY)
        read_limit = min(limit, diagnostic_limit) if diagnostic_limit is not None else limit
        chunks, size = [], 0
        while size <= read_limit:
            if time.monotonic() >= deadline:
                raise GmailFailure(Code.TIMEOUT, True)
            # read1 makes at most one buffered underlying read. Bound that read
            # by the remaining allowance plus one byte to detect overflow/EOF.
            chunk = response.read1(min(65536, read_limit + 1 - size))
            size += len(chunk)
            self.received_bytes += len(chunk)
            if size > limit:
                raise GmailFailure(Code.UNSUPPORTED_CAPABILITY)
            if time.monotonic() >= deadline:
                raise GmailFailure(Code.TIMEOUT, True)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


class GmailReadClient(Protocol):
    def read(self, resource: str, *, params: dict, timeout: float,
             budget: GmailByteBudget | None = None) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GmailHttpClient:
    """Runtime supplies an access-token callable authorized with gmail.readonly.

    No automatic refresh/retry, token logging, redirects, or persistent token cache.
    The caller owns account binding and ensuring the granted scope is readonly.
    """
    def __init__(self, access_token: Callable[[], str], *, max_response_bytes=8_000_000):
        if not callable(access_token) or type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("Token capability and positive response bound required")
        self._token = access_token
        self._limit = max_response_bytes
        self._opener = build_opener(_NoRedirect())

    def read(self, resource, *, params, timeout, budget=None):
        # The source constructs only these read paths. Never accept arbitrary URLs.
        import re
        if not re.fullmatch(r"profile|history|messages(?:/[A-Za-z0-9_-]+(?:/attachments/[A-Za-z0-9_-]+)?)?", resource):
            raise GmailFailure(Code.INVALID_RESPONSE)
        try:
            token = self._token()
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise GmailFailure(Code.AUTH_REQUIRED)
        except Exception:
            raise GmailFailure(Code.AUTH_REQUIRED) from None
        url = "https://gmail.googleapis.com/gmail/v1/users/me/" + quote(resource, safe="/")
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        deadline = time.monotonic() + timeout
        budget = budget if budget is not None else GmailByteBudget(self._limit, self._limit)

        def unique(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("Duplicate JSON fields")
            return value

        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = budget.read_body(response, response_limit=self._limit, deadline=deadline)
            result = json.loads(payload.decode("utf-8"), object_pairs_hook=unique,
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON number")))
            if not isinstance(result, dict):
                raise GmailFailure(Code.INVALID_RESPONSE)
            return result
        except HTTPError as error:
            status = error.code
            reasons = set()
            try:
                body = budget.read_body(error, response_limit=self._limit, deadline=deadline,
                                        diagnostic_limit=65536)
                if len(body) <= 65536:
                    reasons = {e.get("reason") for e in json.loads(body).get("error", {}).get("errors", [])}
            except GmailFailure as failure:
                raise failure from None
            except Exception:
                pass
            finally:
                error.close()
            delay = error.headers.get("Retry-After", "") if error.headers else ""
            delay = min(int(delay), 3600) if delay.isascii() and delay.isdigit() and len(delay) < 8 else None
            if status == 401:
                raise GmailFailure(Code.AUTH_REQUIRED) from None
            if status == 429 or (status == 403 and reasons & {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}):
                raise GmailFailure(Code.RATE_LIMITED, "dailyLimitExceeded" not in reasons, delay) from None
            if status == 403:
                raise GmailFailure(Code.PERMISSION_DENIED) from None
            if status == 404:
                raise GmailFailure(Code.CURSOR_EXPIRED if resource == "history" else Code.NOT_FOUND) from None
            if status in (500, 502, 503, 504):
                raise GmailFailure(Code.UNAVAILABLE, True, delay) from None
            raise GmailFailure(Code.INVALID_RESPONSE) from None
        except (TimeoutError, socket.timeout):
            raise GmailFailure(Code.TIMEOUT, True) from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise GmailFailure(Code.TIMEOUT, True) from None
            if isinstance(error.reason, ssl.SSLError):
                raise GmailFailure(Code.UNAVAILABLE, False) from None
            raise GmailFailure(Code.UNAVAILABLE, True) from None
        except ssl.SSLError:
            raise GmailFailure(Code.UNAVAILABLE, False) from None
        except ConnectionError:
            raise GmailFailure(Code.UNAVAILABLE, True) from None
        except GmailFailure:
            raise
        except Exception:
            raise GmailFailure(Code.INVALID_RESPONSE) from None
