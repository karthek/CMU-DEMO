"""Frozen Gmail FULL body-only normalization. No travel interpretation."""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.parser import HeaderParser
import re

from travel_agent.live.gmail_client import GmailFailure
from travel_agent.live.mail import MailMessage, mailbox, mail_evidence_version
from travel_agent.live.observations import Provenance, RetrievedBy
from travel_agent.live.providers import ProviderErrorCode as Code


def invalid():
    raise GmailFailure(Code.INVALID_RESPONSE)


def unsupported():
    raise GmailFailure(Code.UNSUPPORTED_CAPABILITY)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value):
        invalid()
    return value


def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        invalid()
    return int(value)


def receipt(message):
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=decimal(message.get("internalDate")))


def headers(part):
    values = part.get("headers", [])
    if not isinstance(values, list) or len(values) > 200:
        invalid()
    result = {}
    for item in values:
        if not isinstance(item, dict):
            invalid()
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not re.fullmatch(r"[!-9;-~]+", name) or not isinstance(value, str) or len(value) > 32768:
            invalid()
        value = re.sub(r"\r\n[ \t]+", " ", value)
        if any(c in value for c in ("\r", "\n", "\x00")):
            invalid()
        name = name.lower()
        # Repeated transport headers (notably Received) are normal and not evidence.
        if name not in ("from", "to", "cc", "subject", "content-type", "content-disposition", "content-transfer-encoding"):
            continue
        if name in result and result[name] != value:
            invalid()
        result[name] = value
    return result


def decoded_header(value):
    try:
        words = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\r\n]*)\?=")
        if "=?" in words.sub("", value):
            invalid()
        for match in words.finditer(value):
            encoded = match[3]
            if match[2].lower() == "b":
                base64.b64decode(encoded, validate=True)
            elif re.search(r"=(?![0-9A-Fa-f]{2})", encoded):
                invalid()
        result = "".join(p.decode(c or "ascii", errors="strict") if isinstance(p, bytes) else p
                         for p, c in decode_header(value))
        if "=?" in result or any(c in result for c in ("\r", "\n", "\x00")):
            invalid()
        return result
    except GmailFailure:
        raise
    except Exception:
        invalid()


def addresses(name, value):
    # HeaderRegistry defects reject permissive recovery by email.utils.getaddresses.
    parsed = HeaderParser(policy=policy.default).parsestr(name + ": " + value + "\n\n")[name]
    if parsed.defects or any(g.display_name is not None for g in parsed.groups):
        invalid()
    return tuple(mailbox(a.addr_spec) for a in parsed.addresses)


def decode_data(body, limit):
    if not isinstance(body, dict) or type(body.get("size")) is not int or body["size"] < 0:
        invalid()
    data = body.get("data")
    if not isinstance(data, str) or len(data) > (limit + 2) // 3 * 4 + 4:
        unsupported() if isinstance(data, str) else invalid()
    if body["size"] > limit:
        unsupported()
    if not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data):
        invalid()
    unpadded = data.rstrip("=")
    try:
        raw = base64.b64decode(unpadded + "=" * (-len(unpadded) % 4), altchars=b"-_", validate=True)
    except Exception:
        invalid()
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != unpadded:
        invalid()
    if "=" in data and len(data) % 4:
        invalid()
    if len(raw) != body["size"]:
        invalid()
    return raw


def normalize(message, *, account_id, get_attachment, max_bytes=2_000_000, max_parts=100, max_depth=12):
    """Return complete generic evidence or raise a safe internal failure."""
    message_id = identifier(message.get("id"))
    received = receipt(message)
    root = message.get("payload")
    if not isinstance(root, dict):
        invalid()
    top = headers(root)
    sender = addresses("From", decoded_header(top.get("from", "")))
    if len(sender) != 1:
        invalid()
    recipient_set = set()
    for key in ("to", "cc"):
        if key in top:
            recipient_set.update(addresses(key, decoded_header(top[key])))
    recipients = tuple(sorted(recipient_set))
    leaves, count, used = {}, 0, 0

    def walk(part, depth):
        nonlocal count, used
        count += 1
        if count > max_parts or depth > max_depth:
            unsupported()
        if not isinstance(part, dict):
            invalid()
        h = headers(part)
        mime = part.get("mimeType")
        if not isinstance(mime, str):
            invalid()
        mime = mime.lower()
        metadata = HeaderParser(policy=policy.default).parsestr(
            "\n".join(k + ": " + v for k, v in h.items() if k in ("content-type", "content-disposition")) + "\n\n")
        if any(header.defects for header in metadata.values()):
            invalid()
        if "content-type" in h and metadata.get_content_type() != mime:
            invalid()
        filename = part.get("filename", "")
        if not isinstance(filename, str):
            invalid()
        attachment = bool(filename) or metadata.get_content_disposition() == "attachment"
        if mime == "message/rfc822" or mime in ("multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime"):
            unsupported()
        if attachment:
            return
        if metadata.get_content_disposition() not in (None, "inline"):
            unsupported()
        if h.get("content-transfer-encoding", "7bit").strip().lower() not in ("7bit", "8bit", "binary"):
            unsupported()
        parts = part.get("parts", [])
        if not isinstance(parts, list):
            invalid()
        body = part.get("body")
        if mime.startswith("multipart/"):
            if mime not in ("multipart/mixed", "multipart/alternative", "multipart/related"):
                unsupported()
            if not parts or not isinstance(body, dict) or body.get("size") != 0 or body.get("data", "") or body.get("attachmentId"):
                invalid()
            for child in parts:
                walk(child, depth + 1)
            return
        if parts or mime not in ("text/plain", "text/html"):
            unsupported()
        if not isinstance(body, dict):
            invalid()
        if body.get("attachmentId"):
            if body.get("data"):
                invalid()
            fetched = get_attachment(message_id, identifier(body["attachmentId"]))
            if fetched.get("size") != body.get("size"):
                invalid()
            body = fetched
        raw = decode_data(body, max_bytes - used)
        used += len(raw)
        charset = metadata.get_content_charset()
        codecs = {"utf-8": "utf-8", "us-ascii": "ascii", "iso-8859-1": "iso-8859-1", "windows-1252": "cp1252"}
        if charset is not None and charset.lower() not in codecs:
            unsupported()
        try:
            value = raw.decode(codecs[charset.lower()] if charset else "ascii", errors="strict")
        except UnicodeError:
            invalid()
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if mime in leaves:
            unsupported()
        leaves[mime] = value

    walk(root, 0)
    if not leaves:
        unsupported()
    result = MailMessage(account_id, message_id, "provisional", sender[0],
                         decoded_header(top.get("subject", "")), leaves.get("text/plain", ""), received,
                         html_body=leaves.get("text/html"), provider="GMAIL", recipients=recipients,
                         provenance=Provenance("gmail-api/internal-date/mail-normalization-v1", received, RetrievedBy.PROVIDER))
    return replace(result, version=mail_evidence_version(result))
