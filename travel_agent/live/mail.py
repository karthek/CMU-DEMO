"""Normalized mail evidence shared by offline extraction and future mail adapters."""
from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses
from html.parser import HTMLParser
import re

from travel_agent.live.observations import Provenance, text
from travel_agent.live.time import utc


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Text body required")
    lines = (" ".join(line.split()) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "tr", "li") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "tr", "li") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value)
    parser.close()
    return normalize_text("".join(parser.parts))


def mailbox(value: str) -> str:
    text(value)
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise ValueError("Single mailbox address required")
    _, address = addresses[0]
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address):
        raise ValueError("Single mailbox address required")
    local, domain = address.rsplit("@", 1)
    return local + "@" + domain.lower()


@dataclass(frozen=True)
class MailMessage:
    # Preserve the Phase 2 positional constructor and providers.MailMessage import.
    account_id: str
    message_id: str
    version: str
    sender: str
    subject: str
    text_body: str
    received_at: datetime
    html_body: str | None = None
    provider: str = "UNSPECIFIED"
    thread_id: str | None = None
    recipients: tuple[str, ...] = ()
    provenance: Provenance | None = None

    def __post_init__(self):
        for value in (self.provider, self.account_id, self.message_id, self.version):
            text(value)
        if self.thread_id is not None:
            text(self.thread_id)
        object.__setattr__(self, "sender", mailbox(self.sender))
        if not isinstance(self.subject, str) or "\n" in self.subject or "\r" in self.subject:
            raise ValueError("Single line subject required")
        object.__setattr__(self, "subject", " ".join(self.subject.split()))
        object.__setattr__(self, "text_body", normalize_text(self.text_body))
        object.__setattr__(self, "received_at", utc(self.received_at))
        if self.html_body is not None and not isinstance(self.html_body, str):
            raise ValueError("HTML body must be text")
        if not isinstance(self.recipients, tuple):
            raise ValueError("Immutable recipient tuple required")
        object.__setattr__(self, "recipients", tuple(mailbox(r) for r in self.recipients))
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise ValueError("Typed provenance required")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.provider, self.account_id, self.message_id, self.version

    @property
    def bodies(self) -> tuple[str, ...]:
        return tuple(body for body in (self.text_body, html_text(self.html_body) if self.html_body else "") if body)
