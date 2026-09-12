"""Bounded Gmail evidence source. No repository, extraction, or planning imports."""
from dataclasses import dataclass
from datetime import datetime
import json
import math
import time

from travel_agent.live.gmail_client import GmailReadClient, GmailFailure, GmailByteBudget
from travel_agent.live.gmail_mime import normalize, receipt, identifier, decimal, invalid, unsupported
from travel_agent.live.observations import text
from travel_agent.live.providers import (MailSyncPage, ProviderResult, ProviderError,
                                        ProviderErrorCode as Code, RetainedMailIdentityLookup)
from travel_agent.live.time import utc, parse_instant


@dataclass(frozen=True)
class GmailLimits:
    max_list_pages: int = 100
    max_history_pages: int = 100
    max_requests: int = 20000
    max_items: int = 10000
    max_response_bytes: int = 8_000_000
    max_batch_bytes: int = 32_000_000
    max_mime_bytes: int = 2_000_000
    max_parts: int = 100
    max_depth: int = 12
    attempt_seconds: int = 120
    request_seconds: int = 15

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in vars(self).values()):
            raise ValueError("Positive integer bounds required")
        if self.max_items > 10000:
            raise ValueError("Core batch bound is 10000 items")


class _Attempt:
    def __init__(self, client, limits, clock):
        self.client, self.limits, self.clock = client, limits, clock
        self.deadline = clock() + limits.attempt_seconds
        self.requests = 0
        self.budget = GmailByteBudget(limits.max_response_bytes, limits.max_batch_bytes)

    def check(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise GmailFailure(Code.TIMEOUT, True)
        return remaining

    def read(self, resource, **params):
        remaining = self.check()
        self.requests += 1
        if self.requests > self.limits.max_requests:
            unsupported()
        result = self.client.read(resource, params=params, timeout=min(remaining, self.limits.request_seconds),
                                  budget=self.budget)
        self.check()
        if not isinstance(result, dict) or "error" in result:
            invalid()
        return result

    def pages(self, resource, maximum, **params):
        token, seen = None, set()
        for _ in range(maximum):
            result = self.read(resource, **params, **({"pageToken": token} if token else {}))
            yield result
            token = result.get("nextPageToken")
            if token is None:
                return
            if not isinstance(token, str) or not token or len(token) > 4096 or token in seen:
                invalid()
            seen.add(token)
        unsupported()


class GmailMailSource:
    """One fully buffered bounded core page per attempt, including empty success.

    Runtime binds client and account; retained_identity is only a membership callable.
    No source-local durable state or assumptions that previous results committed.
    """
    PROFILE = "gmail-full-inclusive-nondraft/v1"

    def __init__(self, *, account_id: str, client: GmailReadClient,
                 retained_identity: RetainedMailIdentityLookup, limits=None, monotonic=time.monotonic):
        text(account_id)
        if not callable(retained_identity):
            raise ValueError("Retained identity lookup required")
        self.account_id, self._client = account_id, client
        self._retained, self.limits, self._clock = retained_identity, limits or GmailLimits(), monotonic

    def _cursor(self, since, checkpoint):
        return json.dumps(dict(format="gmail-cursor/v1", account=self.account_id,
                               profile=self.PROFILE, lower_bound=since.isoformat(), checkpoint=checkpoint),
                          sort_keys=True, separators=(",", ":"))

    def _parse_cursor(self, cursor):
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                invalid()
            def unique(pairs):
                values = dict(pairs)
                if len(values) != len(pairs):
                    invalid()
                return values
            data = json.loads(cursor, object_pairs_hook=unique)
            if set(data) != {"format", "account", "profile", "lower_bound", "checkpoint"}:
                invalid()
            if (data["format"], data["account"], data["profile"]) != ("gmail-cursor/v1", self.account_id, self.PROFILE):
                invalid()
            lower = parse_instant(data["lower_bound"])
            if decimal(data["checkpoint"]) <= 0:
                invalid()
            return lower, data["checkpoint"]
        except Exception:
            raise GmailFailure(Code.CURSOR_EXPIRED) from None

    @staticmethod
    def _profile(attempt):
        checkpoint = attempt.read("profile").get("historyId")
        if decimal(checkpoint) <= 0:
            invalid()
        return checkpoint

    def _history(self, attempt, start):
        records, previous, terminal = {}, decimal(start), decimal(start)
        event_count = 0
        for page in attempt.pages("history", self.limits.max_history_pages, startHistoryId=start, maxResults=500):
            current = decimal(page.get("historyId"))
            if current < terminal:
                invalid()
            terminal = current
            history = page.get("history", [])
            if not isinstance(history, list):
                invalid()
            for record in history:
                if not isinstance(record, dict):
                    invalid()
                number = decimal(record.get("id"))
                if number <= decimal(start) or number > current or number < previous:
                    invalid()
                previous = number
                events = set()
                for kind in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                    entries = record.get(kind, [])
                    if not isinstance(entries, list):
                        invalid()
                    for entry in entries:
                        if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
                            invalid()
                        mid = identifier(entry["message"].get("id"))
                        labels = ()
                        if kind.startswith("labels"):
                            labels = entry.get("labelIds")
                            if not isinstance(labels, list) or not labels or any(not isinstance(l, str) or not l for l in labels):
                                invalid()
                            labels = tuple(sorted(set(labels)))
                        events.add((mid, kind, labels))
                generic = record.get("messages", [])
                if not isinstance(generic, list):
                    invalid()
                generic_ids = {identifier(m.get("id")) for m in generic if isinstance(m, dict)}
                if len(generic_ids) and not generic_ids <= {e[0] for e in events}:
                    invalid()  # No untyped change may silently vanish.
                if any(not isinstance(m, dict) for m in generic) or not events:
                    invalid()
                frozen = frozenset(events)
                if number in records and records[number] != frozen:
                    invalid()
                if number not in records:
                    event_count += len(frozen)
                records[number] = frozen
                if event_count > self.limits.max_items:
                    unsupported()
        state = {}
        for number in sorted(records):
            by_message = {}
            for mid, kind, labels in records[number]:
                by_message.setdefault(mid, []).append((kind, labels))
            for mid, events in by_message.items():
                kinds = {k for k, _ in events}
                if "messagesDeleted" in kinds and kinds != {"messagesDeleted"}:
                    invalid()
                added = {l for k, labels in events if k == "labelsAdded" for l in labels}
                removed = {l for k, labels in events if k == "labelsRemoved" for l in labels}
                if added & removed or (state.get(mid) == "deleted" and kinds != {"messagesDeleted"}):
                    invalid()
                state[mid] = "deleted" if "messagesDeleted" in kinds else "fetch"
        return state, str(terminal), bool(records)

    def _message(self, attempt, mid, lower, incremental):
        result = attempt.read("messages/" + mid, format="full")
        if result.get("id") != mid:
            invalid()
        labels = result.get("labelIds", [])
        if not isinstance(labels, list) or any(not isinstance(l, str) or not l for l in labels):
            invalid()
        if "DRAFT" in labels:
            return None
        if receipt(result) < lower:
            if not incremental:
                return None
            retained = self._retained(provider="GMAIL", account_id=self.account_id, message_id=mid)
            if type(retained) is not bool:
                invalid()
            if not retained:
                return None
        return normalize(result, account_id=self.account_id,
                         get_attachment=lambda message, attachment: attempt.read("messages/" + message + "/attachments/" + attachment),
                         max_bytes=self.limits.max_mime_bytes, max_parts=self.limits.max_parts, max_depth=self.limits.max_depth)

    def sync(self, *, since: datetime, cursor: str | None, page_token: str | None = None):
        try:
            if page_token is not None:
                invalid()  # No partial adapter attempt can be resumed.
            since = utc(since)
            attempt = _Attempt(self._client, self.limits, self._clock)
            messages, removals = [], []
            if cursor is None:
                lower, checkpoint = since, self._profile(attempt)
                ids = set()
                query = f"after:{math.floor(lower.timestamp()) - 1} -in:drafts"
                for page in attempt.pages("messages", self.limits.max_list_pages,
                                          q=query, includeSpamTrash="true", maxResults=500):
                    entries = page.get("messages", [])
                    if not isinstance(entries, list):
                        invalid()
                    for entry in entries:
                        if not isinstance(entry, dict):
                            invalid()
                        ids.add(identifier(entry.get("id")))
                        if len(ids) > self.limits.max_items:
                            unsupported()
                for mid in sorted(ids):
                    message = self._message(attempt, mid, lower, False)
                    if message is not None:
                        messages.append(message)
                _, terminal, changed = self._history(attempt, checkpoint)
                if changed or terminal != checkpoint:
                    raise GmailFailure(Code.UNAVAILABLE, True)
            else:
                lower, checkpoint = self._parse_cursor(cursor)
                affected, terminal, _ = self._history(attempt, checkpoint)
                for mid, action in sorted(affected.items()):
                    if action == "deleted":
                        removals.append(mid)
                    else:
                        message = self._message(attempt, mid, lower, True)
                        if message is not None:
                            messages.append(message)
                checkpoint = terminal
            if self._profile(attempt) != checkpoint:
                raise GmailFailure(Code.UNAVAILABLE, True)
            attempt.check()
            return ProviderResult(value=MailSyncPage(tuple(messages), tuple(removals), None, self._cursor(lower, checkpoint)))
        except GmailFailure as failure:
            return ProviderResult(error=failure.error)
        except TimeoutError:
            return ProviderResult(error=ProviderError(Code.TIMEOUT, True))
        except ConnectionError:
            return ProviderResult(error=ProviderError(Code.UNAVAILABLE, True))
        except Exception:
            return ProviderResult(error=ProviderError(Code.INVALID_RESPONSE, False))
