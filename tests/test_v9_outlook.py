"""Offline Graph folder/delta fixtures, deliberately unrelated to airline grammar."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from urllib.parse import urlsplit, unquote

import pytest

from test_v9_graph_client import BINDING, Response, client, no_network
from test_v9_mail_sync import NOW
from travel_agent.live.graph_client import GraphLimits, route_url
from travel_agent.live.graph_normalization import instant
from travel_agent.live.outlook import OutlookMailSource
from travel_agent.live.providers import ProviderErrorCode as Code


LOWER = NOW - timedelta(days=365)


def message(mid="m1", *, parent="inbox", received=NOW):
    return dict(id=mid, receivedDateTime=received.isoformat(), parentFolderId=parent,
        isDraft=False, changeKey="opaque-change", subject="Generic evidence",
        **{"from": {"emailAddress": {"address": "sender@example.test"}},
           "sender": {"emailAddress": {"address": "delegate@example.test"}}},
        toRecipients=[{"emailAddress": {"address": "b@example.test"}}], ccRecipients=[],
        body={"contentType": "text", "content": "Complete message body"},
        hasAttachments=False, internetMessageHeaders=[])


def folder(fid, parent, children=0, **extra):
    return dict(id=fid, parentFolderId=parent, isHidden=False, childFolderCount=children, **extra)


def delta_url(fid, token="complete"):
    return route_url(BINDING, ("mailFolders", fid, "messages", "delta"), "?$deltatoken=" + token)


class MailboxTransport:
    def __init__(self, messages=None, hints=None):
        self.messages = deepcopy(messages if messages is not None else {"m1": message()})
        self.root = folder("root", "outside", 2)
        self.children = {"root": [folder("inbox", "root"), folder("archive", "root")],
                         "inbox": [], "archive": []}
        self.hints = hints if hints is not None else {"inbox": [{"id": mid} for mid in self.messages]}
        self.attachments = {}
        self.delta_calls, self.calls, self.overrides = {}, [], {}
        self.responses = []

    def open(self, url, *, headers, deadline):
        self.calls.append((url, headers, deadline))
        path = tuple(unquote(p) for p in urlsplit(url).path.split("/")[4:])
        query = urlsplit(url).query
        if path in self.overrides:
            override = self.overrides[path]
            value = override(url, query) if callable(override) else override
        elif path == ("mailFolders", "msgfolderroot"):
            value = self.root
        elif path[-1] == "childFolders":
            value = {"value": self.children[path[1]]}
        elif path[-1] == "delta":
            fid = path[1]
            count = self.delta_calls.get(fid, 0)
            self.delta_calls[fid] = count + 1
            value = {"value": self.hints.get(fid, []) if count == 0 else [],
                     "@odata.deltaLink": delta_url(fid, str(count + 1))}
        elif path[-1] == "attachments":
            value = {"value": self.attachments.get(path[1], [])}
        elif path[0] == "messages":
            value = self.messages.get(path[1], Response({}, 404))
        else:
            raise AssertionError("Unexpected fake route")
        response = value if isinstance(value, Response) else Response(deepcopy(value))
        self.responses.append(response)
        return response


def source(transport=None, *, retained=(), limits=None):
    transport = transport or MailboxTransport()
    lookup = retained if callable(retained) else lambda **kw: kw["message_id"] in retained
    return OutlookMailSource(client=client(transport), retained_identity=lookup, limits=limits)


def run(transport=None, *, cursor=None, retained=(), limits=None, since=LOWER):
    return source(transport, retained=retained, limits=limits).sync(since=since, cursor=cursor)


def checkpoint():
    result = run(MailboxTransport(messages={}))
    assert result.error is None
    return result.value.completed_cursor


def failed(result, code):
    assert result.value is None
    assert result.error.code == code


def test_initial_all_folders_full_body_and_vector_checkpoint():
    transport = MailboxTransport()
    result = run(transport)
    assert result.error is None
    page = result.value
    assert page.next_page_token is None and not page.removed_message_ids
    msg, = page.messages
    assert msg.provider == "OUTLOOK_MAIL" and msg.account_id == BINDING.account_id
    assert msg.sender == "sender@example.test" and msg.thread_id is None
    assert msg.version.startswith("mail-evidence/v1:sha256:")
    assert msg.provenance.observed_at == msg.received_at == NOW
    cursor = json.loads(page.completed_cursor)
    assert [f["id"] for f in cursor["folders"]] == ["archive", "inbox", "root"]
    assert all(f["delta_url"] == delta_url(f["id"], "2") for f in cursor["folders"])
    assert transport.delta_calls == dict(archive=2, inbox=2, root=2)
    assert all('IdType="ImmutableId"' in c[1]["Prefer"] for c in transport.calls)
    body_calls = [c for c in transport.calls if "subject,body" in c[0]]
    assert len(body_calls) == 1 and 'outlook.body-content-type="html"' in body_calls[0][1]["Prefer"]
    assert not any("$filter" in c[0] for c in transport.calls)


@pytest.mark.parametrize("retained,inside,expected", [(False, False, 0), (True, False, 1), (False, True, 1)])
@pytest.mark.parametrize("incremental", [False, True])
def test_retained_old_vs_never_retained_and_inclusive_discovery(retained, inside, expected, incremental):
    msg = message(received=LOWER if inside else LOWER - timedelta(seconds=1))
    transport = MailboxTransport({"m1": msg})
    result = run(transport, cursor=checkpoint() if incremental else None, retained=("m1",) if retained else ())
    assert result.error is None and len(result.value.messages) == expected
    assert sum("subject,body" in c[0] for c in transport.calls) == expected


def test_incremental_keeps_initial_lower_bound():
    msg = message(received=LOWER + timedelta(days=1))
    result = run(MailboxTransport({"m1": msg}), cursor=checkpoint(), since=LOWER + timedelta(days=30))
    assert result.error is None and len(result.value.messages) == 1
    assert instant(json.loads(result.value.completed_cursor)["initial_lower_bound"]) == LOWER


def test_same_content_version_stable_metadata_changes_excluded():
    original = run().value.messages[0]
    msg = message()
    msg.update(changeKey="another", lastModifiedDateTime=NOW.isoformat(), internetMessageId="ignored",
               parentFolderId="archive", conversationId="ignored")
    equivalent = run(MailboxTransport({"m1": msg})).value.messages[0]
    assert equivalent.identity == original.identity
    msg["body"]["content"] += " changed"
    changed = run(MailboxTransport({"m1": msg}), retained=("m1",)).value.messages[0]
    assert changed.identity[:3] == original.identity[:3] and changed.version != original.version


def test_empty_lookback_has_real_completed_vector():
    result = run(MailboxTransport(messages={}))
    assert result.error is None and not result.value.messages
    assert len(json.loads(result.value.completed_cursor)["folders"]) == 3


def test_multi_page_delta_empty_next_page_and_duplicates_coalesce():
    transport = MailboxTransport()
    calls = [0]
    next_url = route_url(BINDING, ("mailFolders", "inbox", "messages", "delta"), "?$skiptoken=opaque")
    def page(url, query):
        calls[0] += 1
        return [
            {"value": [], "@odata.nextLink": next_url},
            {"value": [{"id": "m1"}, {"id": "m1", "changeKey": "different"}], "@odata.deltaLink": delta_url("inbox")},
            {"value": [], "@odata.deltaLink": delta_url("inbox", "quiet")},
        ][calls[0] - 1]
    transport.overrides[("mailFolders", "inbox", "messages", "delta")] = page
    result = run(transport)
    assert result.error is None and len(result.value.messages) == 1
    assert sum("subject,body" in c[0] for c in transport.calls) == 1
    assert any(c[0] == next_url for c in transport.calls)


@pytest.mark.parametrize("failure", ["body", "light", "folder"])
def test_fetch_and_folder_failures_never_emit_completion(failure):
    transport = MailboxTransport()
    if failure == "folder":
        transport.overrides[("mailFolders", "archive", "messages", "delta")] = Response({}, 503)
    else:
        transport.overrides[("messages", "m1")] = lambda url, query: (
            Response({}, 503) if failure == "light" or "subject,body" in query else message())
    failed(run(transport), Code.UNAVAILABLE)


@pytest.mark.parametrize("incremental,removed,retained,code", [
    (False, False, False, Code.NOT_FOUND), (False, True, True, Code.NOT_FOUND),
    (True, False, True, Code.NOT_FOUND), (True, True, True, None), (True, True, False, None)])
def test_disappearance_needs_incremental_removal_hint(incremental, removed, retained, code):
    hint = {"id": "m1"} | ({"@removed": {"reason": "deleted"}} if removed else {})
    result = run(MailboxTransport(messages={}, hints={"inbox": [hint]}),
                 cursor=checkpoint() if incremental else None, retained=("m1",) if retained else ())
    if code:
        failed(result, code)
    else:
        assert result.error is None
        assert result.value.removed_message_ids == (("m1",) if retained else ())


def test_inbox_to_archive_removed_plus_added_resolves_one_identity():
    msg = message(parent="archive")
    transport = MailboxTransport({"m1": msg}, {"inbox": [{"id": "m1", "@removed": {"reason": "deleted"}}],
                                            "archive": [{"id": "m1"}]})
    result = run(transport, cursor=checkpoint(), retained=("m1",))
    assert result.error is None and len(result.value.messages) == 1
    assert result.value.removed_message_ids == ()
    gets = [c for c in transport.calls if "/messages/m1?" in c[0]]
    assert len(gets) == 2 and all("mailFolders" not in c[0] for c in gets)


@pytest.mark.parametrize("draft", [False, True])
def test_retained_scope_exit_or_draft_emits_visibility_only(draft):
    msg = message(parent="inbox" if draft else "hidden")
    msg["isDraft"] = draft
    result = run(MailboxTransport({"m1": msg}), cursor=checkpoint(), retained=("m1",))
    assert result.error is None and result.value.removed_message_ids == ("m1",)
    assert result.value.messages == ()


@pytest.mark.parametrize("shape", [{"reason": "changed"}, None, {}, {"reason": "deleted", "extra": 1}])
def test_unknown_removed_shape_fails(shape):
    failed(run(MailboxTransport(hints={"inbox": [{"id": "m1", "@removed": shape}]})), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("bad", [None, [], "yes"])
def test_membership_must_be_boolean(bad):
    failed(run(retained=lambda **kw: bad), Code.INVALID_RESPONSE)


def test_membership_error_is_not_false():
    def broken(**kw):
        raise RuntimeError("private")
    failed(run(retained=broken), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(account_id="other"), lambda c: c.update(provider="GMAIL"),
    lambda c: c.update(normalization_profile="v2"), lambda c: c.update(mode="partial"),
    lambda c: c["folders"][0].update(delta_url="https://evil.example.test/"),
    lambda c: c["folders"].append(c["folders"][0]),
    lambda c: c.update(initial_lower_bound="2026-09-11T00:00:00"),
])
def test_cursor_scope_rejected_before_io(mutation):
    cur = json.loads(checkpoint())
    mutation(cur)
    transport = MailboxTransport()
    failed(run(transport, cursor=json.dumps(cur)), Code.CURSOR_EXPIRED)
    assert not transport.calls


def test_scope_change_requires_full_resync():
    transport = MailboxTransport(messages={})
    transport.root["childFolderCount"] = 1
    transport.children["root"].pop()
    failed(run(transport, cursor=checkpoint()), Code.CURSOR_EXPIRED)


def test_hidden_and_search_folders_excluded_without_alias_discovery():
    transport = MailboxTransport(messages={})
    transport.root["childFolderCount"] += 2
    hidden = folder("hidden", "root")
    hidden["isHidden"] = True
    transport.children["root"].extend([hidden, folder("search", "root", **{"@odata.type": "#microsoft.graph.mailSearchFolder"})])
    result = run(transport)
    assert result.error is None
    assert len(json.loads(result.value.completed_cursor)["folders"]) == 3
    assert not any("/hidden/" in c[0] or "/search/" in c[0] for c in transport.calls)


@pytest.mark.parametrize("case", ["unknown", "cycle", "wrong_parent", "count"])
def test_inventory_validation(case):
    transport = MailboxTransport()
    child = transport.children["root"][0]
    if case == "unknown":
        child["@odata.type"] = "#microsoft.graph.unknown"
    elif case == "cycle":
        child["id"] = "root"
    elif case == "wrong_parent":
        child["parentFolderId"] = "elsewhere"
    else:
        child["childFolderCount"] = 1
    expected = Code.UNSUPPORTED_CAPABILITY if case == "unknown" else Code.UNAVAILABLE if case == "count" else Code.INVALID_RESPONSE
    failed(run(transport), expected)


def test_quiet_validation_replay_aborts():
    transport = MailboxTransport()
    transport.overrides[("mailFolders", "inbox", "messages", "delta")] = {
        "value": [{"id": "m1"}], "@odata.deltaLink": delta_url("inbox")}
    failed(run(transport), Code.UNAVAILABLE)


def test_final_inventory_race_aborts():
    transport = MailboxTransport()
    count = [0]
    def root(url, query):
        count[0] += 1
        return transport.root | ({"parentFolderId": "changed"} if count[0] == 2 else {})
    transport.overrides[("mailFolders", "msgfolderroot")] = root
    failed(run(transport), Code.UNAVAILABLE)


def test_metadata_body_race_aborts():
    transport = MailboxTransport()
    transport.overrides[("messages", "m1")] = lambda url, query: message() | (
        {"changeKey": "changed"} if "subject,body" in query else {})
    failed(run(transport), Code.UNAVAILABLE)


@pytest.mark.parametrize("case", ["repeat", "both", "neither", "wrong_scope", "bad_value"])
def test_pagination_fail_closed(case):
    transport = MailboxTransport()
    def page(url, query):
        if case == "repeat":
            return {"value": [], "@odata.nextLink": url}
        if case == "both":
            return {"value": [], "@odata.nextLink": url, "@odata.deltaLink": url}
        if case == "neither":
            return {"value": []}
        if case == "wrong_scope":
            return {"value": [], "@odata.deltaLink": delta_url("other")}
        return {"value": {}, "@odata.deltaLink": delta_url("inbox")}
    transport.overrides[("mailFolders", "inbox", "messages", "delta")] = page
    failed(run(transport), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("field,limit", [("pages", 1), ("requests", 1), ("folders", 1),
    ("enumerated_folders", 2), ("folder_depth", 1), ("response_bytes", 1),
    ("attempt_bytes", 100), ("body_bytes", 1), ("cursor_bytes", 10)])
def test_bounds_prevent_completion(field, limit):
    failed(run(limits=replace(GraphLimits(), **{field: limit})), Code.INVALID_RESPONSE)


def test_external_page_token_is_unsupported():
    failed(source().sync(since=LOWER, cursor=None, page_token="partial"), Code.UNSUPPORTED_CAPABILITY)


@pytest.mark.parametrize("field", ["from", "subject", "body", "receivedDateTime", "parentFolderId",
                                  "changeKey", "isDraft", "toRecipients", "internetMessageHeaders"])
def test_missing_required_fields_fail(field):
    msg = message()
    del msg[field]
    failed(run(MailboxTransport({"m1": msg})), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("content_type,body", [("text", "  full text\r\n second  line "),
    ("html", "<div>full text</div>\r\n<p>second line</p>")])
def test_complete_body_normalization(content_type, body):
    msg = message()
    msg["body"] = dict(contentType=content_type, content=body)
    result = run(MailboxTransport({"m1": msg}))
    assert result.error is None
    normalized = result.value.messages[0]
    assert normalized.bodies == ("full text\nsecond line",)
    assert (normalized.html_body is None) == (content_type == "text")


@pytest.mark.parametrize("body,code", [(None, Code.INVALID_RESPONSE),
    ({"contentType": "rtf", "content": "stuff"}, Code.UNSUPPORTED_CAPABILITY),
    ({"contentType": "text", "content": ""}, Code.UNSUPPORTED_CAPABILITY),
    ({"contentType": "html", "content": "<script>secret</script>"}, Code.UNSUPPORTED_CAPABILITY)])
def test_no_bodypreview_substitution(body, code):
    msg = message()
    msg.update(body=body, bodyPreview="plausible complete-looking preview")
    failed(run(MailboxTransport({"m1": msg})), code)


@pytest.mark.parametrize("stamp", ["2026-09-11T12:00:00", "not-date", "2026-09-11T12:00:00.1234567Z"])
def test_aware_lossless_timestamp_required(stamp):
    msg = message()
    msg["receivedDateTime"] = stamp
    failed(run(MailboxTransport({"m1": msg})), Code.INVALID_RESPONSE)


def test_offset_timestamp_and_extra_zero_precision_are_lossless():
    assert instant("2026-09-11T08:00:00.1234560-04:00") == instant("2026-09-11T12:00:00.123456Z")


def test_from_does_not_fall_back_to_sender_and_recipients_are_stable():
    msg = message()
    msg["toRecipients"] *= 2
    msg["ccRecipients"] = [{"emailAddress": {"address": "a@EXAMPLE.test"}}]
    result = run(MailboxTransport({"m1": msg})).value.messages[0]
    assert result.sender == "sender@example.test"
    assert result.recipients == ("a@example.test", "b@example.test")
    msg["from"] = None
    failed(run(MailboxTransport({"m1": msg})), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("headers,code", [
    ([{"name": "From", "value": "other@example.test"}], Code.INVALID_RESPONSE),
    ([{"name": "Content-Type", "value": "text/plain"}, {"name": "Content-Type", "value": "text/html"}], Code.INVALID_RESPONSE),
    ([{"name": "Content-Type", "value": "multipart/encrypted"}], Code.UNSUPPORTED_CAPABILITY),
    ([{"name": "Content-Class", "value": "rpmsg.message"}], Code.UNSUPPORTED_CAPABILITY),
])
def test_header_conflicts_and_protected_content(headers, code):
    msg = message()
    msg["internetMessageHeaders"] = headers
    failed(run(MailboxTransport({"m1": msg})), code)


@pytest.mark.parametrize("kind,expected", [("fileAttachment", None), ("itemAttachment", Code.UNSUPPORTED_CAPABILITY),
    ("referenceAttachment", Code.UNSUPPORTED_CAPABILITY), ("unknown", Code.UNSUPPORTED_CAPABILITY)])
def test_attachment_metadata_only(kind, expected):
    transport = MailboxTransport()
    transport.attachments["m1"] = [{"@odata.type": "#microsoft.graph." + kind, "id": "a1",
        "contentType": "image/png", "isInline": True, "size": 100}]
    result = run(transport)
    if expected:
        failed(result, expected)
    else:
        assert result.error is None
    assert not any("$value" in c[0] or "contentBytes" in c[0] for c in transport.calls)


def test_has_attachments_inconsistency_is_not_empty_success():
    msg = message()
    msg["hasAttachments"] = True
    failed(run(MailboxTransport({"m1": msg})), Code.INVALID_RESPONSE)


def test_root_only_mailbox_is_a_valid_single_folder_scope():
    transport = MailboxTransport({"m1": message(parent="root")}, hints={"root": [{"id": "m1"}]})
    transport.root["childFolderCount"] = 0
    transport.children = {"root": []}
    result = run(transport)
    assert result.error is None and len(result.value.messages) == 1
    assert len(json.loads(result.value.completed_cursor)["folders"]) == 1


def test_recursive_paginated_folder_inventory():
    transport = MailboxTransport(messages={})
    transport.children["root"][0]["childFolderCount"] = 1
    transport.children["inbox"] = [folder("nested", "inbox")]
    transport.children["nested"] = []
    def children(url, query):
        if "$skiptoken" in query:
            return {"value": transport.children["root"][1:]}
        return {"value": transport.children["root"][:1], "@odata.nextLink":
            route_url(BINDING, ("mailFolders", "root", "childFolders"), "?$skiptoken=page2")}
    transport.overrides[("mailFolders", "root", "childFolders")] = children
    result = run(transport)
    assert result.error is None
    assert {f["id"] for f in json.loads(result.value.completed_cursor)["folders"]} == {"root", "inbox", "archive", "nested"}


def test_unchanged_empty_delta_link_is_not_a_loop():
    transport = MailboxTransport(messages={})
    for fid in ("root", "inbox", "archive"):
        transport.overrides[("mailFolders", fid, "messages", "delta")] = {
            "value": [], "@odata.deltaLink": delta_url(fid)}
    assert run(transport).error is None


def test_two_link_cycle_is_rejected():
    transport = MailboxTransport()
    def page(url, query):
        return {"value": [], "@odata.nextLink": route_url(BINDING,
            ("mailFolders", "inbox", "messages", "delta"), "?$skiptoken=" + ("a" if query.endswith("b") else "b"))}
    transport.overrides[("mailFolders", "inbox", "messages", "delta")] = page
    failed(run(transport), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("field", ["attempt_bytes", "response_bytes"])
def test_complete_attempt_raw_byte_exact_boundary_and_one_over(field):
    baseline = MailboxTransport()
    assert run(baseline).error is None
    sizes = [len(response.raw) for response in baseline.responses]
    bound = sum(sizes) if field == "attempt_bytes" else max(sizes)
    assert run(limits=replace(GraphLimits(), **{field: bound})).error is None
    failed(run(limits=replace(GraphLimits(), **{field: bound - 1})), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("field", ["delta_entries", "candidate_ids", "emitted_items"])
def test_delta_and_emission_bounds(field):
    transport = MailboxTransport({"m1": message(), "m2": message("m2")})
    failed(run(transport, limits=replace(GraphLimits(), **{field: 1})), Code.INVALID_RESPONSE)


@pytest.mark.parametrize("field", ["recipients", "headers", "header_bytes", "attachments"])
def test_normalization_metadata_bounds(field):
    transport = MailboxTransport()
    msg = transport.messages["m1"]
    msg["toRecipients"] *= 2
    msg["internetMessageHeaders"] = [{"name": "X-Ignored", "value": "some text"}] * 2
    transport.attachments["m1"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "id": aid,
        "contentType": "image/png", "isInline": True, "size": 1} for aid in ("a", "b")]
    failed(run(transport, limits=replace(GraphLimits(), **{field: 1})), Code.INVALID_RESPONSE)


def test_unfiltered_5001_metadata_entries_never_silently_truncate():
    hints = {"inbox": [{"id": f"old-{n}"} for n in range(5001)]}
    transport = MailboxTransport({}, hints)
    # Current gets for old candidates are bounded too: exhausting calls is a
    # visible failure, never the filtered-delta API's silent 5,000-item cutoff.
    original_open = transport.open
    def open(url, *, headers, deadline):
        if "/messages/old-" in url:
            mid = unquote(urlsplit(url).path.rsplit("/", 1)[1])
            transport.messages[mid] = message(mid, received=LOWER - timedelta(days=1))
        return original_open(url, headers=headers, deadline=deadline)
    transport.open = open
    failed(run(transport), Code.INVALID_RESPONSE)
    assert len(transport.calls) == GraphLimits().requests
    assert not any("$filter" in call[0] or "subject,body" in call[0] for call in transport.calls)


def test_changed_account_cursor_cannot_be_reused_even_with_valid_other_session():
    from test_v9_graph_client import Credentials, SESSION
    from travel_agent.live.graph_client import MicrosoftGraphHttpClient
    other = replace(BINDING, tenant_id="00000000-0000-0000-0000-000000000009")
    transport = MailboxTransport()
    bound_client = MicrosoftGraphHttpClient(binding=other,
        credentials=Credentials(replace(SESSION, binding=other)), transport=transport, monotonic=lambda: 0)
    result = OutlookMailSource(client=bound_client, retained_identity=lambda **kw: False).sync(since=LOWER, cursor=checkpoint())
    failed(result, Code.CURSOR_EXPIRED)
    assert not transport.calls


def test_html_and_text_fictional_evidence_extract_identical_facts():
    from html import escape
    from test_v9_mail_sync import mail
    from travel_agent.live.extraction import ItineraryExtractor
    original = mail()
    msg = message("booking")
    msg["subject"] = original.subject
    msg["from"]["emailAddress"]["address"] = original.sender
    msg["body"]["content"] = original.text_body
    text_message = run(MailboxTransport({"booking": msg})).value.messages[0]
    msg["body"] = dict(contentType="html", content="<div>" + escape(msg["body"]["content"]).replace("\n", "<br>") + "</div>")
    html_message = run(MailboxTransport({"booking": msg})).value.messages[0]
    extractor = ItineraryExtractor()
    first = extractor.extract(text_message)
    second = extractor.extract(html_message)
    assert first.state == second.state
    assert first.segment is not None and first.segment == second.segment


@pytest.mark.parametrize("incremental", [False, True])
def test_folder_disappearing_during_initial_inventory(incremental):
    transport = MailboxTransport()
    transport.overrides[("mailFolders", "inbox", "childFolders")] = Response({}, 404)
    failed(run(transport, cursor=checkpoint() if incremental else None),
           Code.CURSOR_EXPIRED if incremental else Code.UNAVAILABLE)


def test_final_inventory_disappearing_is_retryable_failure():
    transport = MailboxTransport()
    calls = [0]
    def inventory(url, query):
        calls[0] += 1
        return transport.root if calls[0] == 1 else Response({}, 404)
    transport.overrides[("mailFolders", "msgfolderroot")] = inventory
    result = run(transport)
    failed(result, Code.UNAVAILABLE)
    assert result.error.retryable
