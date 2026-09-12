"""Strict Graph body profile; no MIME traversal or airline interpretation."""
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
import re

from travel_agent.live.graph_client import require, identifier
from travel_agent.live.mail import MailMessage, mailbox, html_text, mail_evidence_version
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.providers import ProviderErrorCode as Code


LIGHT_SELECT = "id,receivedDateTime,parentFolderId,isDraft,changeKey"
FULL_SELECT = LIGHT_SELECT + ",from,sender,toRecipients,ccRecipients,subject,body,hasAttachments,internetMessageHeaders"


def instant(value):
    require(isinstance(value, str))
    match = re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})", value)
    require(match is not None)
    fraction = match[1] or ""
    require(not any(c != "0" for c in fraction[6:]))
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def metadata(value, expected_id):
    require(isinstance(value, dict) and value.get("id") == expected_id)
    identifier(expected_id)
    parent = identifier(value.get("parentFolderId"))
    require(type(value.get("isDraft")) is bool)
    identifier(value.get("changeKey"))
    return parent, instant(value.get("receivedDateTime")), value["isDraft"], value["changeKey"]


def address(value):
    require(isinstance(value, dict) and isinstance(value.get("emailAddress"), dict))
    return mailbox(value["emailAddress"].get("address"))


def normalize(value, *, binding, expected_id, limits):
    _, received, draft, _ = metadata(value, expected_id)
    require(not draft)
    sender = address(value.get("from"))
    recipients = []
    for name in ("toRecipients", "ccRecipients"):
        require(isinstance(value.get(name), list))
        recipients.extend(value[name])
    require(len(recipients) <= limits.recipients)
    recipients = tuple(sorted(set(address(v) for v in recipients)))
    subject = value.get("subject")
    require(isinstance(subject, str) and "\r" not in subject and "\n" not in subject)
    body = value.get("body")
    require(isinstance(body, dict) and isinstance(body.get("content"), str)
            and isinstance(body.get("contentType"), str))
    kind = body["contentType"].lower()
    require(kind in ("text", "html"), Code.UNSUPPORTED_CAPABILITY)
    content = body["content"]
    require(len(content.encode("utf-8")) <= limits.body_bytes)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    visible = html_text(content) if kind == "html" else content.strip()
    require(bool(visible), Code.UNSUPPORTED_CAPABILITY)
    require(len(visible.encode("utf-8")) <= limits.body_bytes)
    require(type(value.get("hasAttachments")) is bool)
    headers = value.get("internetMessageHeaders")
    require(isinstance(headers, list) and len(headers) <= limits.headers)
    relevant, size = {}, 0
    for header in headers:
        require(isinstance(header, dict) and isinstance(header.get("name"), str)
                and isinstance(header.get("value"), str))
        name, val = header["name"].lower(), header["value"]
        require(bool(name) and "\n" not in name and "\r" not in name)
        size += len((name + val).encode("utf-8"))
        require(size <= limits.header_bytes)
        if name in ("from", "content-type", "content-class"):
            # RFC folding is permitted; bare line breaks are not.
            val = re.sub(r"\r?\n[ \t]+", " ", val)
            require("\r" not in val and "\n" not in val)
            key = mailbox(val) if name == "from" else " ".join(val.lower().split())
            require(name not in relevant or relevant[name] == key)
            relevant[name] = key
    if "from" in relevant:
        require(relevant["from"] == sender)
    if "content-class" in relevant:
        require("rpmsg" not in relevant["content-class"], Code.UNSUPPORTED_CAPABILITY)
    if "content-type" in relevant:
        header = Message()
        header["content-type"] = relevant["content-type"]
        mime = header.get_content_type()
        require(mime not in ("multipart/signed", "multipart/encrypted", "message/rfc822",
                             "application/pkcs7-mime", "application/x-pkcs7-mime",
                             "application/ms-tnef", "application/x-microsoft-rpmsg-message"),
                Code.UNSUPPORTED_CAPABILITY)
        # Graph's requested representation can convert a source text body to HTML.
        # Multipart declarations do not expose child structure; never traverse it.
        require(mime in ("text/plain", "text/html", "multipart/mixed", "multipart/alternative",
                         "multipart/related"), Code.UNSUPPORTED_CAPABILITY)
    message = MailMessage(
        account_id=binding.account_id, message_id=expected_id, version="pending",
        sender=sender, subject=subject, text_body=content if kind == "text" else "",
        received_at=received, html_body=content if kind == "html" else None,
        provider="OUTLOOK_MAIL", recipients=recipients,
        provenance=Provenance("microsoft-graph/received-date-time/mail-normalization-v1",
                              received, RetrievedBy.PROVIDER))
    require(len(message.text_body.encode("utf-8")) <= limits.body_bytes)
    return replace(message, version=mail_evidence_version(message))


def validate_attachments(values, *, has_attachments, limits):
    require(len(values) <= limits.attachments)
    noninline = 0
    ids = set()
    for item in values:
        require(isinstance(item, dict))
        require(item.get("@odata.type") == "#microsoft.graph.fileAttachment", Code.UNSUPPORTED_CAPABILITY)
        aid = identifier(item.get("id"))
        require(aid not in ids)
        ids.add(aid)
        require(isinstance(item.get("contentType"), str) and bool(item["contentType"])
                and type(item.get("isInline")) is bool
                and type(item.get("size")) is int and item["size"] >= 0)
        require(not any(k in item for k in ("contentBytes", "item", "sourceUrl")))
        noninline += not item["isInline"]
    require(has_attachments == bool(noninline))
