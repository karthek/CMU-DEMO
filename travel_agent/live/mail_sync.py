"""Bounded provider-neutral offline synchronization application; no scheduling."""
from dataclasses import dataclass
from datetime import timedelta
import sqlite3

from travel_agent.live.booking import project
from travel_agent.live.config import SynchronizationPolicy
from travel_agent.live.mail import MailMessage
from travel_agent.live.observations import text
from travel_agent.live.providers import MailSyncPage, ProviderResult, ProviderError, ProviderErrorCode
from travel_agent.live.time import utc


@dataclass(frozen=True)
class SyncOutcome:
    success: bool
    cursor: str | None
    error: ProviderError | None = None
    resync_required: bool = False


class MailSynchronization:
    def __init__(self, repository, *, authorized_travelers, extractor=None,
                 projector=project, policy=None, max_pages=100, max_messages=10000):
        if not isinstance(authorized_travelers, frozenset):
            raise ValueError("Configured immutable traveler references required")
        for traveler in authorized_travelers:
            text(traveler)
        if any(type(n) is not int or n < 1 for n in (max_pages, max_messages)):
            raise ValueError("Positive synchronization limits required")
        self.repository, self.authorized_travelers = repository, authorized_travelers
        self.extractor, self.projector = extractor, projector
        self.policy = policy or SynchronizationPolicy()
        self.max_pages, self.max_messages = max_pages, max_messages

    def run(self, source, provider, account_id, *, as_of, full=False):
        text(provider)
        text(account_id)
        as_of = utc(as_of)
        if type(full) is not bool or provider == "UNSPECIFIED":
            raise ValueError("Explicit synchronization scope required")
        before = self.repository.sync_state(provider, account_id)
        cursor = before["cursor"] if before else None
        full = full or cursor is None
        since = as_of - timedelta(days=self.policy.initial_lookback_days)

        def failed(error):
            # Audit is best effort if the DB itself is unavailable/locked.
            try:
                self.repository.record_sync_failure(provider, account_id, expected_state=before, error=error, as_of=as_of)
            except sqlite3.Error:
                pass
            return SyncOutcome(False, cursor, error,
                error.code == ProviderErrorCode.CURSOR_EXPIRED or self.repository.resync_required(provider, account_id))

        if self.repository.resync_required(provider, account_id) and not full:
            return SyncOutcome(False, cursor, ProviderError(ProviderErrorCode.CURSOR_EXPIRED, False), True)
        messages, removed, tokens = [], set(), set()
        page_token = None
        try:
            for _ in range(self.max_pages):
                response = source.sync(since=since, cursor=None if full else cursor, page_token=page_token)
                if not isinstance(response, ProviderResult):
                    raise ValueError("Typed provider result required")
                if response.error:
                    return failed(response.error)
                page = response.value
                if not isinstance(page, MailSyncPage) or not isinstance(page.messages, tuple) or not isinstance(page.removed_message_ids, tuple):
                    raise ValueError("Typed immutable page required")
                if (page.next_page_token is None) == (page.completed_cursor is None):
                    raise ValueError("Exactly one continuation or completed cursor required")
                for token in (page.next_page_token, page.completed_cursor):
                    if token is not None:
                        text(token)
                for message in page.messages:
                    if not isinstance(message, MailMessage) or (message.provider, message.account_id) != (provider, account_id):
                        raise ValueError("Message outside synchronization scope")
                for removed_id in page.removed_message_ids:
                    text(removed_id)
                messages.extend(page.messages)
                removed.update(page.removed_message_ids)
                if len(messages) + len(removed) > self.max_messages:
                    raise ValueError("Synchronization batch limit exceeded")
                if removed & {m.message_id for m in messages}:
                    raise ValueError("Ambiguous visibility within batch")
                if page.completed_cursor is not None:
                    self.repository.commit_sync(provider, account_id, expected_state=before,
                        completed_cursor=page.completed_cursor, messages=tuple(messages), removed_ids=frozenset(removed),
                        full=full, since=since, as_of=as_of, authorized_travelers=self.authorized_travelers,
                        extractor=self.extractor, projector=self.projector)
                    return SyncOutcome(True, page.completed_cursor)
                if page.next_page_token in tokens:
                    raise ValueError("Pagination cycle")
                tokens.add(page.next_page_token)
                page_token = page.next_page_token
            raise ValueError("Pagination limit exceeded")
        except Exception:
            # Store a safe code only: exception text can contain mail or credentials.
            return failed(ProviderError(ProviderErrorCode.INVALID_RESPONSE, False))
