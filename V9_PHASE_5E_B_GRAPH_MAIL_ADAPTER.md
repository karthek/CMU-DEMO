# V9 Phase 5E-B: bounded Microsoft Graph Mail adapter

Implementation and review date: 2026-09-12.
Branch: `feature/v9-live-replanning`.
Parent: `9c7e4e13707745667ac5439b236b7ac1a915d1da`, the authorized Phase 5E-A
documentation freeze. Phase 5E-B remains uncommitted; no push or calendar work.

Authoritative contract:
[V9_PHASE_5E_A_GRAPH_MAIL_CONTRACT.md](V9_PHASE_5E_A_GRAPH_MAIL_CONTRACT.md).
Its official Microsoft references and distinctions between documented behavior
and project policy remain authoritative. That frozen document is unchanged.

## Architecture and implemented scope

```text
GraphCredentialProvider.verified_session()     GraphTransport.open/read
              |                              (absolute I/O deadlines)
              +---------- MicrosoftGraphHttpClient
                                      |
                              OutlookMailSource
                                      |
                         MailMessage + MailSyncPage
                                      |
                          MailSynchronization
                                      |
               immutable evidence / TemplateRegistry / typed events
                                      |
                     BookingRepository projection / readiness
```

The three added source modules are `graph_client.py`, `graph_normalization.py`
and `outlook.py`. No existing source module is modified. Graph code imports no
repository, SQLite, booking, reconciliation, template, Gmail or planner module.
The HTTP client owns safe routing, immutable-ID headers, raw response reads,
JSON validation and neutral errors. The source owns folder inventory, delta
vectors, current mailbox resolution, admission and completion validation.
Normalization owns only the frozen body-evidence representation.

Only own-primary-mailbox global Graph v1.0 read operations are exposed: root and
child-folder inventory, folder message delta, mailbox message get and attachment
metadata listing. Other endpoint families are rejected. There is no OAuth flow,
refresh, application permission, shared mailbox, calendar, worker, scheduler,
MCP, airline adapter or operational-flight implementation.

## Runtime interfaces and account validation

`GraphAccountBinding(tenant_id, principal_id, mailbox_user_id)` is immutable.
All components must be canonical GUID strings; mailbox_user_id must equal the
principal object ID for this own-mailbox profile. Its account_id is exactly
`graph-global:<tenant-id>:<principal-object-id>`.

The client requires a `GraphCredentialProvider` implementing
`verified_session() -> GraphAccessSession`. This is a **trusted runtime port**:
the runtime must validate the issuer, audience, expiry and nonce of the identity
session and bind its verified tenant/principal, mailbox and delegated Graph scopes
to the ready access token from that same session. Constructing a binding/session
value alone is not authentication. There is no caller-supplied `verified=True`
flag or access-JWT decoding in the adapter.

Each attempt requests a fresh validated session and compares the complete binding
with the configured expected binding before mail I/O. Missing, mismatched or
failed validation returns AUTH_REQUIRED. The Graph delegated scope set must be
exactly `https://graph.microsoft.com/Mail.Read`; missing or broader Graph scopes
fail PERMISSION_DENIED. OIDC openid/profile and optional offline_access belong
to the external identity runtime, not that Graph resource scope set. An
application-only token cannot legitimately satisfy this delegated credential port.

The token is runtime-only, excluded from the session repr, and used solely for
the Authorization header. No access/refresh token, raw exception, HTTP diagnostic
or challenge is returned to synchronization or persisted in mail/cursor JSON.
There is no claim of consumer, guest or production identity-runtime validation:
an account class must remain unsupported until its runtime proves this binding.

## Transport integration and deadlines

`MicrosoftGraphHttpClient` requires an injected `GraphTransport`:

```text
open(url, *, headers, deadline) -> GraphResponse
GraphResponse: status, headers
read(maximum, *, deadline) -> bytes
close() -> nonblocking resource cancellation/release
```

Open means GET with no redirects, automatic retries or decompression. The transport
must enforce the supplied **absolute monotonic deadline**, including name
resolution, connection, headers and each read. It must return at most the requested
bytes and release/cancel resources on close. The client also checks the deadline
before and after calls, after bounded JSON processing and before final emission.

No concrete network transport or login runtime is installed in this slice.
This is an intentional use of the injected transport allowed by 5E-A, not a claim
that a default urllib inactivity timeout meets the deadline contract. A host must
supply and validate the deadline-capable transport and verified credential port
before making live calls. Offline tests exercise those ports with fake responses,
timeouts, advancing monotonic clocks and nonconforming chunks; they do not prove
a particular real transport's DNS/header cancellation behavior.

## Identity and immutable evidence version

Every HTTP read, including inventory, delta and continuation, sends
`Prefer: IdType="ImmutableId"`. Exact case-sensitive message IDs survive folder
moves within the bound primary mailbox. Folder IDs are synchronization scope,
never message/booking identity. Copies, archive-mailbox transfers and re-imports
are not heuristically merged.

Evidence is `(OUTLOOK_MAIL, account_id, immutable_message_id, version)`.
Version is the unchanged `mail_evidence_version()` result:
`mail-evidence/v1:sha256:<digest>`. changeKey, folder, read state, internetMessageId,
conversation ID, delta URLs, fetch times and provider modification times are not
evidence versions. They cannot authorize travel events. Rereads with identical
normalized evidence have identical versions; changed content produces a new
immutable version under the same retained identity.

## Initial synchronization and folder scope

The `graph-normal-folders/v1` profile includes msgfolderroot itself and every
non-hidden physical descendant, including normal Inbox, Archive, user, Sent,
Junk and Deleted Items folders. It excludes search-folder aliases and hidden
subtrees. Draft messages are excluded wherever found. Folder IDs and parents,
not localized display names, determine scope. Unknown types fail unsupported.

Inventory recursively drains child-folder pages with includeHiddenFolders=true,
validates IDs/parents/flags/counts, and rejects cycles, duplicates and limits.
Even a reported zero child count is checked by listing. Count/list disagreements
are retryable inventory races. Included topology changes against a saved vector
require CURSOR_EXPIRED; disappearance during full inventory or final validation
is retryable UNAVAILABLE. No permission or size failure silently narrows coverage.

Full synchronization uses **unfiltered** metadata delta in every included folder,
selecting id, receivedDateTime, parentFolderId, isDraft and changeKey. No filtered
delta, date-ordering shortcut or fabricated current token is used. Each traversal
must reach a real terminal deltaLink. This can enumerate old metadata and exceed
bounds in a large mailbox; such an attempt fails visibly.

Affected IDs are coalesced and resolved through `/users/{bound-id}/messages/{id}`.
Full bodies are fetched only for eligible receipt >= the supplied initial since
(365 days by the existing default synchronization policy) or retained membership.
The lower bound is inclusive and remains fixed in the incremental cursor. A new
full resync establishes a new supplied boundary. A disappearing full-sync candidate
fails NOT_FOUND. No body or checkpoint is silently omitted.

## Delta, completion validation and cursor envelope

Incremental synchronization drains each stored folder delta URL and every nextLink.
Entries are unordered hints, not ordered events. Repeated or disagreeing hints
coalesce by exact ID; the current mailbox get determines the final candidate state.
The light get and subsequent full get must agree on ID, parent, receipt, draft and
changeKey; disagreement is retryable UNAVAILABLE, not a guessed current body.

After body and attachment validation, drain exactly one additional delta round
for every folder. Any change, removal or replay aborts as retryable UNAVAILABLE.
Then inventory the folders again and require identical included topology. Persist
the terminal URLs from the empty validation rounds, which may differ from earlier
URLs. A repeated terminal on an empty round is valid; a nonadvancing terminal
with nonempty changes is not completion.

Only then return one sorted, disjoint `MailSyncPage`, with next_page_token=None.
An external page_token is unsupported. There is no intermediate folder checkpoint
or partial core page. The quiet pass detects observed races; it is not a common
mailbox snapshot or a guarantee against delayed provider changes.

Cursor TEXT is deterministic JSON with exactly:

- format=`graph-mail-cursor/v1`, provider=`OUTLOOK_MAIL`, account_id;
- api_origin=`https://graph.microsoft.com`, api_version=`v1.0`, mailbox_user_id;
- id_profile=`ImmutableId`, normalization_profile=`graph-mail-normalization-v1`;
- discovery_profile=`graph-normal-folders/v1`, mode=`completed-folder-delta-vector`;
- initial_lower_bound as aware UTC ISO text;
- folders sorted by exact ID, each `{id, parent_id, delta_url}`.

Only complete provider-supplied delta URLs appear in the vector. The adapter
validates exact schema, account, profiles, topology, sizes and routing before
cursor-driven I/O. Invalid/mismatched envelopes return CURSOR_EXPIRED. URL routing
accepts the tested slash and OData key-predicate forms for the exact bound resource;
opaque query state is followed and stored verbatim. Unsafe host/user/folder/path,
userinfo, fragments, ambiguous escapes and pagination loops fail closed. Unknown
legitimate URL variants need explicit coverage, not heuristic reconstruction.

## Moves, removals and retained membership

Folder `@removed: {reason: deleted}` is a mailbox-resolution hint. If a current
mailbox get finds the same immutable ID in another monitored folder, emit one
present message and no removal. Retained messages now outside scope or in draft
state produce visibility removal only. An incremental explicit removed hint plus
mailbox-level 404 produces removal only if membership was retained. Other 404s
fail the attempt. Permission/timeouts never prove absence.

**MAIL REMOVAL != FLIGHT CANCELLATION.** Immutable evidence, canonical events and
booking history remain. A return to visible mail can replay existing evidence;
it cannot implicitly reinstate cancelled travel.

The source accepts the unchanged `RetainedMailIdentityLookup` callable:
`(*, provider, account_id, message_id) -> bool`. Compose
`repository.has_retained_mail_identity` at the application boundary. The repository
queries existing immutable evidence, independently of version and travel state.
The adapter receives no repository object or SQL access. Errors/non-bools fail
the attempt; they never mean false. Since content work is coalesced once per ID
and validation changes abort, there is no second same-attempt admission path
requiring a separately mutable membership cache.

Previously retained old IDs remain processable. Never-retained old IDs are not
newly admitted because delta or a folder move mentions them. Reconstruction uses
persisted membership and the same cursor, not source-object memory.

## Body, headers, recipients and time

Full gets select exactly the fields frozen in 5E-A and send the fixed HTML body
preference. Actual text becomes normalized text_body; actual HTML is preserved
with CRLF/CR to LF conversion in html_body and empty text_body. Existing MailMessage
visible-text conversion remains the template boundary. No DOM reserialization,
remote/cid loads, preview/uniqueBody substitution or Gmail MIME traversal occurs.

From.emailAddress.address is required and normalized through mailbox(). Sender
and Reply-To are never fallbacks. To+Cc normalize to sorted unique addresses;
Bcc and display names are not persisted. Subject must be present and single-line.
Selected structural headers are bounded; disagreeing From/duplicate content
declarations and recognized protected/signed/encrypted/nested content fail closed.
A raw text MIME declaration need not equal Graph's requested converted HTML type.

Each admitted body also requires a complete bounded attachment metadata traversal,
even when hasAttachments=false. Ordinary file attachments, including inline images,
are excluded from evidence. Item/reference/unknown types, missing metadata and
inconsistent attachment flags fail. No attachment payload or nested item is fetched.
Empty visible/attachment-only bodies and unsupported types are unsupported capability;
missing/malformed bodies are invalid response. Arbitrary nonempty mail may still
be NOT_TRAVEL or UNRESOLVED downstream; body retrieval is not itinerary extraction.

Receipt must be losslessly representable aware ISO time. Extra fractional digits
are accepted only if all beyond microseconds are zero. Receipt becomes UTC received_at
and provenance observed_at, with source
`microsoft-graph/received-date-time/mail-normalization-v1` and RetrievedBy.PROVIDER.
The existing synchronization/repository boundary rejects receipt after its as_of
atomically. No mailbox timestamp supplies flight time or airline authority.

## Bounds and neutral failures

`GraphLimits` implements the inclusive 5E-A defaults: 4 MiB per response, 64 MiB per
attempt; 500 requests; 400 collection pages; 20,000 raw delta entries; 10,000 unique
IDs and emitted items; 100 monitored folders; 200 total enumerated entries across
both inventories; hierarchy depth 20; body 1 MiB; 32 attachment metadata entries
per message; 500 To+Cc recipients; 200 headers/64 KiB; JSON depth 32; URL 16 KiB;
cursor 256 KiB; page-size hint 100; attempt 120 seconds/request min(20, remaining).
No automatic retries, payload downloads or nested attachment traversal.

`GraphAttempt` owns counters. Reads request at most 64 KiB or remaining+one probe,
whichever is smaller. Actual consumed bytes, including rejected overflow probes
and error bodies, count across every request and verification step. Overflow is
rejected before buffering/parsing that chunk. Exact limits require bounded EOF
confirmation. JSON reserialization and Content-Length are not measurements.
Compression is rejected; response header names are case-insensitive. Duplicate JSON
keys, nonfinite constants, malformed UTF-8/JSON and excess nesting fail validation.

| Failure | Neutral result |
|---|---|
| Session/401 or insufficient_claims | AUTH_REQUIRED, nonretryable |
| Permission/403 or wrong Graph scopes | PERMISSION_DENIED, nonretryable |
| Message 404 / non-delta 410 | NOT_FOUND, except scoped removal resolution |
| Delta 410 / recognized syncStateNotFound / established folder loss | CURSOR_EXPIRED |
| 429/509 | RATE_LIMITED, retryable, validated Retry-After seconds or None |
| 409/412, transient 5xx, connection/TLS failure, observed quiet/inventory race | UNAVAILABLE, retryable |
| 504, transport timeout, absolute deadline | TIMEOUT, retryable |
| Unsupported body/attachment/endpoint, 501 | UNSUPPORTED_CAPABILITY |
| Malformed data/unsafe links/resource bounds/other invalid request | INVALID_RESPONSE |

No raw provider error text escapes. HTTP-date Retry-After conversion is not
implemented; delay-seconds is supported and no sleeps/retries occur. Budget/deadline
exhaustion precedes interpretation of incomplete error data. Independent attempts
always get fresh counters and revalidated sessions.

## Persistence, recovery and cross-provider behavior

Existing provider_sync_state TEXT holds the vector unchanged. Provider I/O finishes
before the unchanged MailSynchronization/BookingRepository transaction. Successful
evidence, extraction/events, projection, visibility and checkpoint commit atomically.
Failed fetches, normalization, quiet validation, bounds or projection cannot advance
the old cursor or retain partial new evidence.

CURSOR_EXPIRED from any required folder creates the existing persistent
FULL_RESYNC_REQUIRED. Restart and later failures preserve it. Ordinary incremental
attempts are blocked before source I/O; only a successful explicit full sync across
the entire required scope can clear the requirement in its commit. No reset URL
is followed automatically, and no exactly-once claim is introduced.

Gmail and Graph evidence remain separately keyed and traceable. The existing
reconciler can link equivalent reviewed fictional booking facts to one canonical
event with two evidence links. The provider layer does not deduplicate by subject,
sender, timestamp or internetMessageId. Booking lifetime, cancellation, reinstatement
and planning-readiness logic are unchanged.

## Offline validation and limitations

New tests: `test_v9_graph_client.py`, `test_v9_outlook.py` and
`test_v9_graph_persistence.py`. Fixtures are fabricated in those test modules;
booking flows reuse existing explicitly fictional Northstar evidence. No real
mailbox, real account credentials, PII, real airline fixture or network access.

Coverage includes byte/probe boundaries, padding/Unicode, fresh budgets, deadlines,
HTTP and error mapping, account/URL isolation, immutable headers, folder hierarchy
and pagination, full/delta admission, duplicate coalescing, current-body races,
quiet verification, moves/removals, body/header/attachment limits, exact cursor
persistence, restart/full-resync recovery, transaction rollback, cross-provider
convergence, provider-neutral extraction and unchanged cancellation safeguards.
A 5,001-ID bootstrap fixture confirms bounds fail visibly without filtered-delta
truncation. All provider tests prohibit network access.

Final validation (2026-09-12):

- Graph focused: **193 passed in 2.58s**.
- Existing mail/extraction/reconciliation/persistence/booking/provider regressions:
  **145 passed + 23 subtests in 9.95s**.
- Phase 5C Gmail (130) and Phase 5D-A template framework (42):
  **172 passed in 4.81s**.
- Complete suite: **701 passed + 245 subtests in 45.74s**, no failures or skips.

Final diff/whitespace checks include all untracked additions and passed. The
eight-file scope contains only this report, the plan, three new provider modules
and three new test modules; the index is empty. Secret/PII and generated-artifact
review found only invented account IDs, example.test addresses and the explicit
offline token placeholder. No dependency or migration files changed. All 62 Python
source/test files present in the v8 tag are unchanged; all existing source/tests
and the frozen 5E-A contract are unchanged against the Phase 5E-A commit.

Architectural conclusion: **A. PHASE 5E-B GRAPH MAIL ADAPTER APPROVED FOR COMMIT**
for the bounded adapter with the explicit injected runtime prerequisites above.
This does not authorize a Phase 5E-B commit or live mailbox access.

No migration or dependency was added. No frozen Gmail, template, booking, repository,
V7/V8 source or existing test was modified. No frozen 5E-A semantic deviation was
needed. The transport and credential integration requirements above remain explicit
runtime prerequisites; offline tests are not live Microsoft conformance validation.
Large/active mailboxes, replaying validation rounds, unsupported attachments and
unrepresentable timestamps may fail under the deliberate frozen limits. There is
no production mailbox, universal airline, operational-status or live-replanning claim.

The next slice should review and validate a concrete host credential/transport
integration before any live mailbox use, under separate authorization. Phase 5D-B
real-airline validation still requires sanitized format-faithful actual messages.
Calendar and other V9 domains have not begun.
