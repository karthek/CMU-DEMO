# V9 Phase 5E-A: Microsoft Graph Mail contract and semantics review

Review date: **2026-09-12**. Conclusion:
**A. PHASE 5E-A GRAPH MAIL CONTRACT APPROVED FOR COMMIT**.
This is a documentation-only contract recommendation, not user approval to commit
or implement 5E-B. No Graph runtime, OAuth or mailbox calls were implemented.

Verified repository `model_agnostic_travel_agent_v1`, branch
`feature/v9-live-replanning`, HEAD
`0717350898aa1ce1b1d5c4cc7d0244bb489db79f`, initially clean.
V1-V8 and V9 5A-5D-A remain frozen; real-airline Phase 5D-B remains deferred.

## 1. Objective and scope

Map Microsoft Graph's different mechanics into the existing `MailSource`, without
making booking/planning code understand Graph. Review official public documentation;
retain immutable evidence, atomic checkpoints, explicit resync and deterministic
template/event authority. No source changes are needed for this contract.

No HTTP client, mail adapter, OAuth/refresh implementation, live mailbox access,
calendar, FlightAware, Routes, scheduler/worker, MCP/HITL, migrations, booking model,
airline parser, Gmail modification or live replanning is included. Read-only means
no marking read, moving, deleting, sending or changing messages.

**Microsoft** below identifies documented behavior with a reference. **Decision**
identifies project policy/inference, not a Microsoft guarantee. All numerical
project bounds, normalized mappings and consistency algorithms are Decisions.
References were reviewed on the date above; no third-party source defines this
contract. Links to Microsoft SDK reference documentation describe fields only;
SDK behavior is not used as a transport or synchronization guarantee.

## 2. Official references and review date

All entries: reviewed **2026-09-12**, Microsoft Graph v1.0 unless stated otherwise.

| ID | Official documentation | Used for |
|---|---|---|
| M1 | [Immutable Outlook IDs][M1] | Move stability, exceptions, per-request opt-in |
| M2 | [Message delta guide][M2] | Per-folder bootstrap, filters and 5,000-message cap |
| M3 | [message: delta][M3] | Endpoints, query options, removal/move caveat |
| M4 | [Delta overview][M4] | Opaque state, replay, delays, resets and token lifetime |
| M5 | [Message resource][M5] | Mail fields, sender roles, receipt timestamps |
| M6 | [List messages][M6] | Mailbox listing, select/filter and paging |
| M7 | [Get message][M7] | Full selected content, body preferences, headers, MIME |
| M8 | [mailFolder resource][M8] | Well-known folders, Archive distinction |
| M9 | [List mailFolders][M9] | Root-only enumeration, hidden/search folders |
| M10 | [List childFolders][M10] | Recursive hierarchy enumeration |
| M11 | [List message attachments][M11] | Attachment inspection and permission |
| M12 | [Attachment resource][M12] | Attachment type hierarchy |
| M13 | [fileAttachment][M13] | File content/metadata |
| M14 | [itemAttachment][M14] | Nested message/contact/event attachments |
| M15 | [itemBody][M15] | Text/HTML representation |
| M16 | [Permissions reference][M16] | Delegated Mail.Read versus Mail.ReadBasic |
| M17 | [OIDC scopes][M17] | openid/profile and offline_access |
| M18 | [ID token claims][M18] | Stable principal, tenant and account binding |
| M19 | [Get user][M19] | /me identity lookup alternative and User.Read |
| M20 | [Graph errors][M20] | HTTP/error categories and safe code interpretation |
| M21 | [Throttling][M21] | 429, Retry-After, backoff |
| M22 | [Paging][M22] | Following complete continuation URLs |
| M23 | [Move message][M23] | Source removal/destination copy mechanics |
| M24 | [Copy message][M24] | Copies versus moving an item |
| M25 | [Outlook message processing][M25] | Safe HTML/body representation |
| M26 | [mailboxItem: delta][M26] | Alternate admin API is also folder-scoped |
| M27 | [Official message field reference][M27] | changeKey changes when item changes |

## 3. Graph message model and stable identity

**Microsoft:** Default Outlook item IDs can change on folder moves. With
`Prefer: IdType="ImmutableId"`, an item's ID remains stable inside its mailbox.
Moving to an archive mailbox or export/re-import changes it. IDs are case-sensitive;
the preference must accompany every applicable request, including delta/continuations.
Container IDs already have stable regular IDs. Delta URLs support both ID forms. [M1]

**Decision:** Use existing provider spelling **`OUTLOOK_MAIL`**, already present in
`ProviderKind` and cross-provider tests, rather than adding `microsoft_graph` as an
alias that could fork retained identity. The implementation technology is Microsoft
Graph; the evidence key is:

`(provider="OUTLOOK_MAIL", account_id, message_id=immutable Graph id, evidence_version)`.

Retained membership omits evidence_version. Store IDs exactly; no lowercasing,
base64 decoding, folder prefix, RFC Message-ID substitution or heuristic ID format
detection. Opt in from the first request. Never mix regular/immutable ID profiles;
unknown cursor ID profiles require full resync, not silent translation/rekeying.
No existing real Graph evidence needs conversion in this repository.

Copies, re-imports and cross-mailbox representations may represent similar email
with different source identities. Keep them distinct. A same-mailbox move under
the selected ID profile must not create new retained membership. [M23][M24]
PNR/traveler/coupon association remains the existing downstream responsibility.
Graph ID, conversationId/index, internetMessageId, folder, changeKey, subject and
timestamps are never canonical booking/segment identity or airline sequence.

## 4. Evidence version and provider metadata

**Microsoft:** changeKey is an item revision and changes on item changes; delta can
include read-state changes. Neither describes only retained itinerary content.
bodyPreview is limited text, and uniqueBody omits content shared with the conversation.
These are not interchangeable with the full body. [M27][M3][M5]

**Decision:** Reuse `mail_evidence_version()` unchanged:
`mail-evidence/v1:sha256:<lowercase digest>`. Its frozen canonical JSON covers exactly
provider, account_id, message_id, sender, subject, text_body, html_body, received_at,
thread_id, ordered recipients, and provenance(source, observed_at, retrieved_by).
Its version input is excluded. Call after normalization; never emit a provisional
version. No Graph-specific hash algorithm or new serialized mail field is needed.

Graph normalization profile `graph-mail-normalization-v1` fixes:

- provider OUTLOOK_MAIL; thread_id=None;
- receipt and provenance observed_at from receivedDateTime in UTC;
- provenance source `microsoft-graph/received-date-time/mail-normalization-v1`;
  retrieved_by=PROVIDER; this names receipt-based evidence, not actual fetch time;
- sender from the From field and sorted unique To/Cc recipients, as in section 11;
- full text or HTML body mapping in section 10.

Exclude changeKey/ETag, lastModifiedDateTime, createdDateTime, sentDateTime,
internetMessageId/headers, conversationId/index, parentFolderId, categories,
isRead, flags, importance, webLink, links/tokens, fetch time and transport formatting
from this normalized profile. Request only needed metadata; do not persist an
unbounded raw Graph object. Attachment bytes are not part of supported evidence.
Metadata inspection used to reject unsupported content does not become travel data.

Identical normalized evidence has the same version even after a move/read toggle.
Any retained field change produces a new version. Same semantic HTML rendered with
different markup can produce another version; do not hide that difference. Microsoft
does not promise byte-identical safe HTML across service revisions. A reread alone
does not refresh provenance or supply operational freshness. No fingerprint orders
travel events, and no historical rows are rehashed.

## 5. Initial synchronization and lookback/delta interaction

**Microsoft:** A folder delta round can bootstrap and then support incremental
tracking. Date filters support receivedDateTime ge/gt, but filtered delta returns
at most 5,000 messages. The default message delta has no guaranteed sort order.
An ordinary mailbox-wide message list exists, but is not a mailbox-wide message
delta checkpoint. [M2][M6]

**Decision:** Use one combined delta bootstrap, with **unfiltered metadata delta**
per monitored physical folder. No `$filter`, `$orderby`, `$search` or `changeType`
restriction. Initial request:

`GET /v1.0/users/{bound-mailbox-user-id}/mailFolders/{folder-id}/messages/delta`

with `$select=id,receivedDateTime,parentFolderId,isDraft,changeKey` and a small
page-size preference. IDs are always requested/validated. changeKey here is only
change-detection metadata; the frozen version helper still fingerprints every body
we accept. Carry immutable-ID preference on every page. Delta payloads are hints,
not authoritative complete content; use mailbox-scoped get for candidate resolution.

This avoids the filtered-delta cap and avoids filtering away retained old messages
after a later full resync moves the lower bound. Do not assume `latest` token
bootstrap for mail from another resource's documentation. [M4]

Full attempt stages:

1. Bind runtime account, fix `since` (default service lookback 365 days), deadline,
   normalization/discovery profile and complete monitored folder inventory.
2. Traverse each folder's initial delta until its deltaLink. Stage bounded IDs and
   metadata only. Include metadata from older years if Graph returns it; **do not
   materialize those years of body evidence**. If even metadata exceeds bounds,
   fail the attempt visibly. This availability limit is preferable to a falsely
   complete date-filtered traversal.
3. Coalesce all IDs across folders before body retrieval. Resolve current metadata
   by immutable ID via `/users/{bound-id}/messages/{id}` where hints are incomplete
   or conflicting. For visible non-drafts, fetch full selected content only when
   receipt >= fixed since, or previously retained membership is true. Never infer
   receipt from sent/created time. Fetch all admitted content before core emission.
4. Apply the bounded validation pass in section 7. Require unchanged folder scope
   and a completed validation round for every folder. Only then emit one complete
   `MailSyncPage` with the entire cursor vector. Empty lookback is not a shortcut
   around delta/bootstrap/folder validation.

Full fetch disappearance is NOT_FOUND (no successful partial scan). Increments
have the narrowly supported removal resolution described below. Old retained mail
that is still present can be reprocessed without newly admitting never-retained
old mail. The service's new rolling since does not narrow an existing incremental
cursor; use the cursor's fixed discovery lower bound until explicit full resync.

A list-with-date-filter followed by an unrelated delta bootstrap/current token
cannot prove gap-free coverage. It is not the selected two-stage strategy. If an
alternative is later needed for large mailboxes, it requires its own reviewed
coverage protocol; no optimization or migration is authorized here.

## 6. Incremental delta and coalescing

**Microsoft:** Delta returns additions/updates/removals in one or more pages.
nextLink continues a round; deltaLink terminates it. State is opaque, and duplicates
and replays are possible. Empty data with nextLink is not completion. The state
encodes original query options. [M3][M4]

**Decision:** Validate the whole cursor/account/profile/folder vector before I/O.
Follow each stored complete deltaLink for the same folder and drain all nextLinks.
Collect affected immutable IDs and whether a folder-removal marker occurred.
Never apply an arrival-order last-write-wins rule to unordered delta payloads or
compare changeKeys lexicographically. Deduplicate content work by ID; duplicate
identical hints are harmless, disagreeing hints require a current get. A current
get must return exactly the requested ID and a valid scoped parent/receipt/draft
state. A missing required field is not an empty value or permission to guess.

Resolve all affected IDs at mailbox scope, including tombstone IDs when needed to
distinguish folder exit from current presence. Filter eligible body admission by
fixed lower bound OR retained membership. Newly discovered old IDs remain excluded,
even if created/moved/updated delta mentioned them. A same-attempt admitted ID is
also recognized in the attempt's staging set; no persistent membership write is
performed during provider retrieval.

Emit removal IDs only for previously retained or same-attempt admitted identities.
An unresolvable never-retained tombstone does not require a new local visibility row.

Only the final per-ID outcome reaches the core: normalized present evidence OR
removal, never both. Sort output by exact immutable ID for deterministic offline
results. Unsupported/inconsistent responses fail the entire attempt. There is no
Graph event journal or intermediate historical body reconstruction claim.

## 7. Checkpoint validation and consistency limits

**Microsoft:** Delta state represents a resource round, but propagation delays and
replays occur. This does not establish a common atomic snapshot over independently
traversed folders, folder inventory and message gets. [M4]

**Decision:** After staging/resolving content, immediately drain one validation
delta round for **every** monitored folder from its staged terminal link. If any
round contains a change/removal/replay, abort as retryable UNAVAILABLE; do not loop
until quiet. Then re-enumerate the folder inventory and require the same included
IDs/type/parent/hidden profile. Store the terminal links from the empty validation
rounds; do not require their opaque strings to equal previous links.

Any observed scope change while bootstrapping/validating, body/metadata race or
inconsistent identity fails closed. No synthetic checkpoint, lastModified cutoff,
delta token ordering or Gmail-style profile history comparison is invented.

This bounded quiet-pass policy is a **project inference**, not snapshot isolation.
Delayed provider changes may appear in a later round despite an empty check. A
change after a folder's validation is covered by a later poll of that folder's
saved state; there is no single mailbox-wide instant. Successful means all required
traversals and local validation completed for the declared scope, not guaranteed
real-time complete mailbox truth. Neither this review nor 5E-B may advertise live
freshness or production snapshot coverage. If a consumer requires that stronger
guarantee, stop for a new contract rather than loosening this qualification.

## 8. Folder scope

**Microsoft:** Message delta is per mailFolder; the folder hierarchy must be tracked
individually. Root folder listing is not recursive and can contain search folders.
Hidden folders need explicit enumeration options. Archive (one-click folder) differs
from Exchange's archive mailbox. [M2][M8][M9][M10]

**Decision:** First profile `graph-normal-folders/v1` monitors the primary mailbox's
normal physical hierarchy: msgfolderroot itself and all non-hidden physical
descendants. Include Inbox, Archive, user folders, Sent Items, Junk Email and Deleted
Items; exclude messages with isDraft=true wherever they reside. Do not choose Inbox
only or Inbox+Archive and imply mailbox-wide discovery. No airline/subject filter.
Inventory hidden folders as metadata to identify exclusions; do not ingest hidden
system/recoverable stores, search-folder aliases, archive mailboxes, shared/delegated
other-user mailboxes or other accounts. Unknown folder types fail unsupported.

Enumerate actual IDs and parent/hidden/type metadata recursively with pagination,
depth, cycle and count bounds. Use well-known names to resolve system roots, not
localized display names. Search folders are views and must not duplicate physical
message discovery. Subtrees under hidden folders are outside this profile.

Each independent attempt checks the inventory. An included folder added/removed,
moved into/out of scope, or configuration change relative to a stored cursor means
CURSOR_EXPIRED and explicit full resync. A topology race in an already running full
attempt means UNAVAILABLE. No single-folder checkpoint updates are committed.
Folder renaming without identity/topology/scope change is not evidence mutation.

A finite explicit folder allowlist could be another future versioned profile, but
is not a silent fallback if full normal-folder coverage is too large or forbidden.
Permission/bound failures cannot downgrade scope. The reviewed alternate
`/admin/exchange/.../folders/.../items/delta` is also folder-scoped and uses a different
MailboxItem permission model; it is not the requested mailbox-wide shortcut. [M26]
No supported mailbox-wide **message delta** is established for this delegated profile.

## 9. Removal, tombstones and moves

**Microsoft:** Message delta can emit `@removed` with reason `deleted` for deletion
**or movement out of a folder**; events outside a date filter can also appear.
Do not apply general directory-object removed-reason rules to infer mail hard
deletion. [M3] The move operation removes the source representation and creates a
destination representation; immutable-ID mode supplies the same-mailbox continuity
required here. [M23][M1]

**Decision:** A folder tombstone is a resolution hint, not immediate account removal.
Combine source/destination hints, then resolve current mailbox presence:

| Current resolution | Core result |
|---|---|
| Same ID present in monitored scope, eligible receipt/membership, non-draft | One normalized message, no removal; move cannot duplicate retained identity. |
| Present outside monitored scope or now draft, previously retained | Scoped visibility removal only. |
| Incremental ID has explicit removed hint and mailbox get returns 404 | Removal only; means unavailable in this observed scope, not proof of hard delete. |
| 404 without a corresponding removed hint, or any full-sync candidate disappearance | Fail NOT_FOUND; do not silently skip. |
| Authentication/permission/timeout/5xx during resolution | Fail attempt; none proves absence. |
| Old never-retained ID outside admission boundary | No new evidence; no need to create local visibility for an unknown excluded ID. |

Unknown removed shapes/reasons or mutually incompatible unresolved outcomes fail
INVALID_RESPONSE. A later observed restoration/move back may restore visibility and
replay the same evidence. It cannot reinstate cancelled travel by itself.

Reuse removed_message_ids and existing PROVIDER_REMOVED/ABSENT_FROM_FULL_SYNC audit.
No precise hard-delete-versus-move reason is promised by that neutral contract.
Evidence/events/history remain immutable. Full absence still applies only to known
receipt >= current full since. Older retained records not observed in a full scan
retain historical visibility; do not label them freshly verified present. No new
global absence sweep or travel cancellation is added.
**MAIL REMOVAL != FLIGHT CANCELLATION.**

## 10. Body/content normalization and attachment boundary

**Microsoft:** itemBody supplies content and a text/HTML discriminator. A body-type
preference can request text or HTML; Graph also exposes MIME via `$value`. Safe HTML
is a provider representation. [M15][M7][M25] File attachments and attached Outlook
items are different resources, and hasAttachments excludes inline-only attachments.
An itemAttachment can wrap a message, event or contact. [M13][M14][M5]

**Decision:** No MIME parser or `$value` endpoint in the first Graph profile. Get
selected full body with fixed `Prefer: outlook.body-content-type="html"` plus the
immutable ID preference. The full content get selects
`id,receivedDateTime,parentFolderId,isDraft,changeKey,from,sender,toRecipients,ccRecipients,subject,body,hasAttachments,internetMessageHeaders`.
Other fields reviewed above need not be requested just to discard them. Lightweight
current-state resolution can select only id/receipt/folder/draft/changeKey before
admission. Validate actual contentType; normalize an actual text
body as text_body with html_body=None, or an actual HTML body as html_body (CRLF/CR
to LF only), text_body="". Existing MailMessage.bodies/html_text supplies deterministic
visible text to templates. Do not fetch a second converted alternative, collapse
HTML whitespace, reserialize DOMs, render scripts or fetch remote/cid resources.
The same fixed preference is used across rereads and continuations that return bodies.

Never substitute bodyPreview or uniqueBody for body. Missing/null/malformed body
is INVALID_RESPONSE. Unsupported type, known encrypted/protected/nested message
representation or an attachment-only/empty visible body is UNSUPPORTED_CAPABILITY.
No truncated fragment is emitted as complete. Nonempty unsupported text/layout
can remain UNRESOLVED in the existing extractor; provider success is not a booking.

Inspect bounded attachment metadata for each admitted candidate through
`/users/{id}/messages/{id}/attachments?$select=id,contentType,isInline,size`, with
all pages and type discriminators validated. Do not infer no attachments solely
from hasAttachments=false. [M11][M12]
Do not download contentBytes, `$value`, or expand nested item bodies. Ordinary
file attachments (including inline images) are explicitly excluded from this
body-evidence profile; no PDF/OCR/image/document extraction. If only attachments
carry the itinerary, the body cannot be marked a complete extracted itinerary.
itemAttachment, reference/cloud attachments and unknown types fail unsupported
even if a body exists, keeping nested evidence out of this first profile.
Attachment traversal depth=0 and payload-download bytes=0. An empty metadata list
with inconsistent hasAttachments=true is invalid, not proof of completeness.

Request internetMessageHeaders on full reads for bounded structural validation of
relevant MIME/content declarations. Reject recognized signed/encrypted/nested
content requiring interpretation outside the body profile; do not implement MIME
traversal to repair it. Header or body ambiguity fails closed. No raw headers or
attachment metadata are added to immutable MailMessage JSON.

## 11. Sender, recipients and headers

**Microsoft:** From names the represented sending mailbox; Sender identifies the
account generating the message and may differ in delegation scenarios. Reply-To is
a reply destination. Graph exposes To/Cc/Bcc and optional selected internet headers.
receivedDateTime and sentDateTime have different meanings. [M5][M7]

**Decision:** Map `from.emailAddress.address` to MailMessage.sender, matching Gmail's
From-based profile. Require one valid address; no fallback to Sender, Reply-To,
recipient or display name. From != Sender is legitimate metadata, not automatic
spoofing proof; it does not switch the normalized sender identity. Both are claims
from mail, not cryptographic sender authentication. Authenticated airline-origin
requirements remain a later separately reviewed neutral contract.

Normalize sorted unique addresses from toRecipients + ccRecipients only. Do not
infer Bcc or envelope recipients; accessible bccRecipients remain excluded in this
profile, and their absence proves nothing. Recipients do not identify authorized
travelers. Subject uses existing single-line normalization; an explicit empty
string is valid, but a missing selected property is malformed.

internetMessageId is neither retained identity nor cross-provider deduplication.
Do not persist arbitrary headers, transport paths or personal display names. If
relevant selected headers are present and disagree with the projected sender or
content declaration, fail validation rather than choose a preferred duplicate.
Display-name differences alone are irrelevant; address comparisons use mailbox().

## 12. Timestamps

**Microsoft:** receivedDateTime, sentDateTime, createdDateTime and
lastModifiedDateTime are DateTimeOffset values documented in UTC ISO 8601. [M5]

**Decision:** receivedDateTime is required mailbox receipt, mapped to aware UTC
received_at and receipt-based provenance observed_at. Use the V9 time contract,
reject naive/invalid/future receipt values and do not assume America/New_York.
Do not substitute created, sent, modified or fetch time. Nonzero sub-microsecond
precision unrepresentable by the frozen Python model must fail validation rather
than silently round across the discovery boundary; extra zero precision is lossless.
This representability limit is project policy, not a claim about Graph precision.

Other source timestamps remain optional transient provider metadata; do not add
new persisted fields now. Attempt `as_of` belongs to existing sync audit. None of
these timestamps determines flight departure, timezone, authoritative ordering,
lifetime/reinstatement or operational freshness. Airline content supplies travel
times through the deterministic extraction contract.

## 13. Account/mailbox identity and permission boundary

**Microsoft:** Stable ID-token claims include oid and tenant context tid; human
readable names/emails/UPNs may change and are unsuitable identity keys. profile
enables object-ID claims with openid. /me user lookup is an alternative requiring
its own user-read permission. [M18][M17][M19]

**Decision:** Own primary mailbox only, global Graph v1.0. Runtime supplies a
verified binding from the same authorized identity session:
`account_id = graph-global:<tid>:<oid>` (canonical GUID components), and the bound
mailbox user ID used in `/users/{id}` requests. For this own-mailbox profile the
runtime must establish that it is the same principal, not a display-name/UPN guess.
Do not parse Graph access tokens as an application identity contract. Use verified
ID-token/authentication-library identity; issuer/audience/expiry/nonce validation
belongs to runtime, not an airline parser. A missing/mismatched binding fails before
mail retrieval. Do not silently substitute the /me string or an email address.

Work/school and personal Microsoft account **permissions** are documented; runtime
support for each account class still needs binding tests. Do not force a consumer
or guest mailbox into an unverified organizational ID mapping. If runtime cannot
prove the binding, that account class remains unsupported for 5E-B until reviewed;
no alias merging or extra User.Read scope by default. Account rename preserves the
bound key; mailbox replacement/rebinding or another tenant requires explicit review.
Cloud/tenant/principal plus provider prevents cross-account collisions. Shared,
delegated-other-user and application-wide enterprise access are excluded.

**Microsoft:** Delegated Mail.Read reads the signed-in mailbox; Mail.ReadBasic
excludes body/attachments and is insufficient here. Application Mail.Read has a
different, broader access model. Refresh-token issuance on the v2 endpoint requires
explicit offline_access. Tenant policies may still restrict consent. [M16][M17]

**Decision:** Require delegated `https://graph.microsoft.com/Mail.Read`; never
Mail.ReadWrite, Mail.Send, calendar, Mail.Read.Shared, application permissions or
admin-consent architecture. Runtime identity may request openid/profile for the
binding; no email scope is necessary. offline_access is only for a separately
authorized runtime that needs refresh across access-token lifetimes; 5E-B's injected
ready-token client need not implement it. No OAuth flow, token cache/refresh or
background process is approved by 5E-A. Access/refresh tokens stay runtime-only,
outside evidence, cursor, ordinary tables, exception text and logs.

## 14. Pagination safety

**Microsoft:** Follow the complete supplied nextLink/deltaLink, carrying required
headers. Query state is encoded in the link; do not extract a skip number or rebuild
the continuation query. An empty page can still require continuation. [M22][M3]

**Decision:** Opaque state is not permission to send credentials to arbitrary URLs.
Validate HTTPS, exact allowed global Graph host/port, v1.0 endpoint family and the
bound mailbox/folder scope before following; reject userinfo, fragments, controls,
ambiguous escapes, traversal, unexpected path variants and off-host redirects.
Support observed/documented slash and OData key-predicate path forms only with
unambiguous exact IDs. These are validation of routing, not interpretation of token
internals; use the original accepted URL string verbatim for the request.

Never invent token query fields or append select/filter to a continuation. A token
itself cannot be decoded to prove its internal tenant/folder context; bind its
envelope, route and originating request, and rely on server validation as well.
Unknown legitimate URL variants fail until explicitly covered, not guessed.
Persisted cursor links cannot redirect or override Authorization/runtime binding.

Track exact visited nextLinks across each traversal, including its initial URL;
detect A->B->A, repeats, non-progress, missing/both terminal markers, duplicate JSON
keys, wrong value shape and excessive page count. deltaLink may legitimately repeat
between empty independent rounds; that is not a pagination loop. Non-advancing
delta state with nonempty changes must fail UNAVAILABLE rather than imply new
coverage. No SDK hidden paging or retries may bypass bounds.

## 15. Throttling, retry and error mapping

**Microsoft:** 429 signals throttling; honor Retry-After before retrying. Where no
delay is supplied, backoff is recommended. Limits vary; no universal safe polling
rate is promised. HTTP status and machine-readable nested error codes, not human
error messages, define error handling. [M21][M20]

**Decision:** First 5E-B client has **zero automatic retries per request/attempt**.
Return safe retryability/delay to the caller for a later independent attempt; no
worker/sleep loop or refresh-on-401. Thus there is no partial throttle checkpoint,
and the absolute deadline cannot be reset by retries. Any future retry policy must
be bounded within the same byte/call/deadline budget and separately reviewed.
Use valid nonnegative Retry-After delay-seconds; an HTTP-date form may be converted
with an injected aware clock if implemented/tested. Malformed/absent delay yields
None, never an immediate hidden retry or guessed value. Transport error bodies
are read under the same limits, and raw messages/tokens never escape.

Use **actual frozen enum names**, not suggested aliases AUTHENTICATION,
INVALID_DATA or PROVIDER_FAILURE:

| Condition | ProviderErrorCode | retryable | Action |
|---|---|---|---|
| 400 malformed request/payload, not recognized reset | INVALID_RESPONSE | false | Fix contract/configuration; no checkpoint. |
| 401 missing/invalid auth; recognized reauthentication claims challenge | AUTH_REQUIRED | false | Runtime reauthorization; no token persistence. |
| 403 permission/license denial | PERMISSION_DENIED | false | No narrower silent folder fallback. |
| 403 insufficient_claims | AUTH_REQUIRED | false | Runtime challenge handling; no raw challenge into core. |
| 404 message | NOT_FOUND | false | Only removal resolution in section 9 consumes this into a tombstone. |
| Required folder missing from established delta scope | CURSOR_EXPIRED | false | Whole account full resync required. |
| 409/412 transient state conflict on allowed read | UNAVAILABLE | true | Retry whole attempt later; not travel conflict. |
| 410 on delta, or documented delta syncStateNotFound reset in a 40x body | CURSOR_EXPIRED | false | Persist FULL_RESYNC_REQUIRED; do not follow reset Location automatically. |
| Other 410 resource absence | NOT_FOUND | false | No blanket reset outside delta context. |
| 429 (or documented bandwidth throttle) | RATE_LIMITED | true | Carry validated Retry-After. |
| 500/502/503 and transient service 5xx | UNAVAILABLE | true | Whole-attempt retry; carry valid delay if present. |
| 504 or network/absolute deadline timeout | TIMEOUT | true | Discard staged attempt. |
| 501/unsupported account/body/attachment capability | UNSUPPORTED_CAPABILITY | false | No silent omission. |
| Other connection/TLS/service failure | UNAVAILABLE | true | Never expose library exceptions. |
| Malformed JSON, duplicate fields, unsafe links, invalid payload | INVALID_RESPONSE | false | No completion cursor. |
| Resource byte/page/item/depth limit | INVALID_RESPONSE | false | Existing bounded-resource model, no new error category. |
| Quiet validation detects changes | UNAVAILABLE | true | No retry loop inside attempt. |
| Invalid/mismatched cursor envelope | CURSOR_EXPIRED | false | Explicit full recovery. |

Reset classification needs both delta context and a documented recognized code/status;
generic 400/404 is not automatically expiration. Read bounded nested error codes,
prefer the most specific recognized one; unknown codes use safe HTTP mapping.
Budget/deadline failure while reading an error response takes precedence over an
unvalidated body. No partial evidence or cursor commits on any failure.

## 16. Response and attempt bounds

**Decision: proposed initial 5E-B defaults**, not Microsoft guarantees or Gmail
numbers copied by assumption. Small pages bound JSON overhead; whole-folder metadata
and multiple current gets justify distinct attempt limits. Fail visibly when a
mailbox cannot finish; do not truncate or declare a narrower complete scope.

| Resource | Proposed inclusive limit |
|---|---|
| Single HTTP response body | 4 MiB received bytes |
| All HTTP response bodies in one sync attempt | 64 MiB, including errors and verification |
| HTTP calls / provider pages | 500 calls / 400 collection pages across all folders |
| Raw delta entries / unique candidate IDs | 20,000 / 10,000 |
| Emitted messages + removals | 10,000, within existing service limit |
| Folder inventory / hierarchy depth | 100 physical monitored folders / 20 levels; bound all enumerated entries to 200 |
| Body content | 1 MiB UTF-8 before normalization and after conversion |
| Attachment metadata | 32 entries per candidate; 0 payload downloads; 0 nested-item depth |
| Recipient / header metadata | 500 To+Cc addresses; 200 headers and 64 KiB aggregate header text |
| JSON depth | 32, validated before normal processing |
| Link / complete cursor | 16 KiB each / 256 KiB UTF-8 total |
| Page-size hint | 100 items; still enforce actual bounds |
| Absolute attempt deadline / request cap | 120 seconds monotonic / min(20 seconds, remaining attempt time) |
| Post-fetch consistency rounds / automatic retries | One round per folder / zero |

Enforce actual transport-body bytes with an attempt-local reader before unrestricted
JSON parsing. Read only bounded chunks; a one-byte overflow probe can establish
exhaustion but is never buffered/accepted as valid content. Count actual consumed
bytes, including the probe, never json.dumps size or Content-Length estimates.
Exact boundary succeeds only after bounded EOF confirmation. Request identity
content encoding and reject unexpected compression in the first profile, preventing
an unbounded transparent-decompression path. If compression is later supported,
both wire and expanded bytes require explicit bounds.

Deadline applies to connect, headers, body chunks, normalization, all pages/gets,
attachment metadata, inventory verification and final emission. Streaming must not
turn a socket inactivity timeout into an unbounded total read. JSON input is already
byte-bounded before decoding; recursion/parse failures are neutral INVALID_RESPONSE.
No byte state or partial cursor survives a completed/failed independent attempt.

## 17. Opaque cursor persistence and full resync

**Microsoft:** Complete delta URLs must be retained for their resource collection.
Outlook token lifetime is cache-dependent rather than a fixed number of days;
expiration may return syncStateNotFound and resets may return 410 with Location. [M4]

**Decision:** Serialize a private versioned JSON envelope as the existing opaque
`provider_sync_state.cursor` TEXT, keyed by OUTLOOK_MAIL/account_id. Conceptual
fields (not an implemented wire schema):

```text
format: graph-mail-cursor/v1
provider: OUTLOOK_MAIL
account_id: bound cloud/tenant/principal key
api_origin: https://graph.microsoft.com
api_version: v1.0
mailbox_user_id: verified own-mailbox ID
id_profile: ImmutableId
normalization_profile: graph-mail-normalization-v1
discovery_profile: graph-normal-folders/v1
initial_lower_bound: aware UTC timestamp
mode: completed-folder-delta-vector
folders: sorted [{id, parent_id, delta_url: exact complete provider deltaLink}]
```

Store selected scope configuration/version too if made configurable. Only completed
folder links appear; no partial nextLink state or message cache is persisted in the
envelope. Folder state is adapter-owned JSON, not repository columns/travel semantics.
Scope validation checks provider/account/cloud/mailbox/folder set/mode/ID profile/
normalization version and limits before requests. URLs remain opaque secrets-adjacent
sync state: protect the database, never log/share them, never embed OAuth tokens.
The envelope is scope binding, not a signature or new trust mechanism.

`MailSynchronization` already collects all core pages then atomically calls
`BookingRepository.commit_sync`. The adapter should return one coalesced bounded
page, `next_page_token=None`, completion only after all folder work succeeds;
non-null external page_token is explicitly unsupported in the first profile.
All provider pages remain internal. Repository retains cursor strings verbatim in
provider_sync_state and existing sync-run history; schema has no folder restriction
or narrow cursor column requiring migration. **No migration or new port required.**

CURSOR_EXPIRED from any required folder invalidates the whole vector. Existing
persistent FULL_RESYNC_REQUIRED blocks ordinary increments across restart. No implicit
full fallback or automatic use of a reset Location. Explicit full=True rebuilds
all scoped delta states; failure preserves old cursor/requirement. Only the complete
successful full transaction clears it. An incremental success cannot clear/bypass
it. Existing compare-and-commit protects against concurrent stale checkpoints.

## 18. Retained identity, cross-provider rules and authority

Reuse `RetainedMailIdentityLookup(*, provider, account_id, message_id) -> bool`
unchanged. Compose `repository.has_retained_mail_identity` into the source, not
SQLite/BookingRepository itself. Immutable persisted mail evidence is membership
truth regardless of version or visibility. Errors propagate as safe attempt failure,
never False. No cache table, folder lookup, travel answer or membership migration.

For an old ID mentioned by delta/move: true permits continued processing; false
prevents discovery solely due to that mention. ID stability removes the same-mailbox
folder-move identity problem; copies/archive-mailbox/re-import IDs do not inherit
retention by subject, RFC ID or content similarity. A new in-lookback copy can be
independently retained and reconciled later.

Gmail and Graph evidence remain distinct, with provider/account/message/version
provenance. No provider-layer deduplication by internetMessageId, subject, sender or
time. Equivalent extracted booking facts may converge through exact canonical
identity and existing event/lifetime rules; their fingerprints need not match.
Extraction sees only normalized MailMessage, selected versioned rule and typed
result. No Graph fields enter TemplateRegistry, Critic, Beam Search or replanning.

Mail is booking evidence, not operational flight authority. Explicit supported
email cancellation can yield a permitted booking-cancellation event through the
template policy; delay/gate/terminal/actual departure/operational cancellation still
belong to the separate operational authority domain. No LLM or sender-domain-only
authority; later receipt is not reinstatement; mail removal is not cancellation.

## 19. Gmail versus Graph: same internal contract

Gmail column summarizes frozen local Phase 5B/5C, not new Google research. Graph
column references the Microsoft behavior/project policies established above.

| Concern | Gmail | Microsoft Graph | Provider-neutral abstraction |
|---|---|---|---|
| Stable ID | Message.id | ImmutableId preference, mailbox-limited continuity | Scoped message_id, not booking identity |
| Evidence version | Frozen normalized content hash | Same helper/profile-specific normalized inputs | mail-evidence/v1 SHA-256 |
| Initial sync | Lookback list/get + quiet history check | Unfiltered folder metadata bootstrap + admission lookback + quiet validation | Full bounded batch |
| Incremental | Account history records | Per-folder delta vector | MailSource.sync(cursor) |
| Cursor/checkpoint | Scoped history envelope | Scoped complete delta URLs per folder | Opaque cursor TEXT, atomic commit |
| Expiration | Expired history | Delta reset/cache expiration | CURSOR_EXPIRED |
| Pagination | Gmail page token | Complete nextLink | Adapter drains internally |
| Removals | Permanent removal/visibility handling | Resolve folder removed hints at mailbox scope | Visibility only; immutable evidence retained |
| Bodies | Bounded MIME traversal | Selected full itemBody text/HTML | Normalized MailMessage bodies |
| Sender | From mailbox | from.emailAddress.address | Same normalized sender meaning |
| Receipt | internalDate | receivedDateTime | Aware UTC receipt-based provenance |
| Attachments | Bounded selected body leaves; excluded non-body files | Metadata inspection; body-only, no item nesting | Explicit supported profile; no partial evidence |
| OAuth | gmail.readonly | Delegated Mail.Read | Runtime credentials, no token persistence |
| Throttling | Existing neutral failure | RATE_LIMITED + validated Retry-After | ProviderResult/ProviderError |
| Byte/deadline | Actual-byte bounded attempts | Same safety principle, separate policy defaults | No incomplete checkpoint |
| Recovery | Explicit qualifying full required | Entire folder vector full rebuild | Persistent FULL_RESYNC_REQUIRED |

## 20. Proposed 5E-B architecture and implementation gates

Recommend **OutlookMailSource** for the MailSource implementation: it matches
OUTLOOK_MAIL and expresses the mailbox capability. **MicrosoftGraphHttpClient**
names its transport explicitly. This naming choice adds no core abstraction.

```text
Microsoft Graph v1.0
  -> MicrosoftGraphHttpClient (runtime credentials, URL checks, bounded I/O/errors)
  -> OutlookMailSource (folder/delta state, coalescing, normalization, membership)
  -> MailMessage + MailSyncPage
  -> MailSynchronization -> immutable evidence + TemplateRegistry extraction
  -> explicit TemplateEventAuthorityPolicy -> canonical events/projection/readiness
```

The future client should accept an injected transport, ready-token supplier,
verified mailbox binding, monotonic clock and attempt budget. Read operations are
folder listing, message delta, message get and attachment metadata listing only.
Its validated Graph responses are consumed by the source; no raw byte accounting
in the mail source and no Graph state interpreter in BookingRepository.
The source accepts account binding/client/read-only membership/configured limits
and implements the existing sync signature. Private modules/dataclasses may be
introduced only for these responsibilities, not airline-specific services.

5E-B requires separate user implementation authorization, then must prove:

1. Accepted URL forms, ID preference on every request, account binding for each
   claimed account class, fixed normalization and folder profile.
2. Complete recursive scope enumeration, metadata delta with selected changeKey,
   current-ID move resolution and bounded quiet validation as specified.
3. All failure paths preserve the checkpoint and full-resync flag; no partial
   folder commits, hidden retries or unbounded reads.
4. Body/attachment unsupported cases fail closed and no OAuth/token refresh or
   airline parser gets added by implication.
5. Offline fixtures use invented transport IDs/URLs and existing fictional template
   evidence, never personal mailbox content or real state tokens.

No database/interface blocker was found: folder vectors fit opaque cursor storage,
stable identity fits existing keys, membership is already neutral, coalescing fits
one disjoint page, and the existing atomic transaction/resync mechanism applies.
If implementation disproves any mapping or requires migration, changed identity,
weaker resync/projector semantics or Gmail regression, stop and report the smallest
contract correction. Do not redesign around it in 5E-B.

## 21. Offline test matrix for Phase 5E-B (not implemented here)

| Area | Required deterministic cases |
|---|---|
| Identity | Every list/get/delta/continuation sends immutable preference; exact case-sensitive IDs; same-mailbox move; copies/re-import distinct; reject default-ID cursor profile; account/tenant isolation. |
| Evidence | Reread same normalized data same hash; subject/body/recipient changes new hash; read/category/folder/changeKey-only changes same hash; fixed provenance; raw JSON padding/escaping does not alter content hash but does consume transport budget. |
| Full | One/many pages/folders; empty folder and empty lookback with valid terminal vector; disappearing get; failed page; missing fields; exact 365-day boundary; bounded old metadata without body download; >5,000 metadata entries prove no filtered-delta shortcut. |
| Retention | Previously retained old ID processed, never-retained old ID excluded, new recent allowed; membership independent of version; lookup failure aborts; restart uses persisted evidence. |
| Delta | Adds/updates/removals; empty pages plus continuation; empty terminal; duplicate/replayed/conflicting hints; current get resolves final state; multiple folders coalesce one ID; no ordered-changeKey assumption. |
| Consistency | Nonempty validation round aborts; inventory changes abort/invalidate; delayed/replayed provider changes processed on later round; no claim of global snapshot; changed terminal URL accepted on empty validation. |
| Folders | Recursive/paginated child discovery; root mail; renamed localized folder; search alias excluded; hidden subtree excluded; count/depth/cycle failures; required new/deleted folder forces whole-vector resync; no Inbox-only fallback. |
| Moves | Monitored->monitored, monitored->unmonitored, return to monitored; source removal before/after destination entry; old never-retained destination add; uncovered 404 fails; removed+404 gives visibility only; Deleted Items move does not cancel flight. |
| Recovery | 410 and recognized syncStateNotFound; unrelated 400/404 not reset; persistent flag across restart; failed full preserves it; qualifying full clears it; incremental cannot clear/bypass; invalid envelope/changed profile never silently resets. |
| Links | Repeated nextLink, A-B-A, empty-page loop, malformed/off-host/wrong-user/wrong-folder URL, redirect, excessive pages/link/cursor bytes, both/neither next/delta, opaque token preserved exactly, repeated empty terminal accepted. |
| Bodies | Actual text/HTML normalization; fixed preference; missing/null/type-invalid/oversized body; preview/uniqueBody never substituted; conflicting header addresses; MIME/protected/nested content unsupported; no scripts/remote resource fetching. |
| Attachments | Bounded metadata pages including inline-only false hasAttachments; excluded ordinary file; attachment-only body unsupported; item/reference types unsupported; zero nested/payload fetches; count/size inconsistency fails. |
| Time/sender | Offset-aware receipt, fractional precision representability, naive/future/missing receipt rejected; From versus Sender distinction; no Reply-To fallback; To/Cc sorting; no Bcc/traveler inference. |
| Errors/retry | 400/401/403/404/409/410/429/5xx; valid/malformed Retry-After; bounded nested code interpretation; malformed JSON/UTF-8, duplicate keys, library exceptions neutralized; zero automatic retries. |
| Bounds | Raw padded/escaped response larger than reserialized JSON; exact per-response/attempt limit and +1; error responses counted; many folders/pages accumulate; independent attempt resets; no parsing after overflow; stalled/chunked deadline; unexpected compression; no completion after exhaustion. |
| Repository | Failed page/fetch/normalization/quiet check/projection doesn't advance evidence/cursor; complete vector commits atomically; restart preserves URLs; stale concurrent commit rejected; failure never erases immutable evidence; full absence stays lookback-scoped. |
| Cross-provider | Distinct Gmail/Graph evidence and hashes; equivalent authorized fictional booking facts converge downstream; no internetMessageId dedup; parser has no Graph dependency; all Gmail, framework, lifetime/reinstatement and V8 regressions green. |

## 22. Uncertainties and explicit availability limits

- No all-folder transactional snapshot, fixed delta-cache lifetime or bounded
  replication-delay guarantee was established. Quiet validation detects observed
  races only. Never present partial/failed traversals or old visibility as current.
- Unfiltered metadata bootstrap may exceed bounds in large/active mailboxes.
  Filtered delta's cap is not an acceptable invisible fallback. Continuous activity
  or harmless replays can cause retryable validation failure.
- `$select` metadata plus changeKey is based on documented item version semantics;
  5E-B must test that client fixtures represent body-only and metadata updates and
  must not assume selected fields grant an ordering proof or recover unseen revisions.
  No live service conformance or production coverage is claimed by offline tests.
- Runtime must verify own-mailbox binding for each supported organizational,
  consumer or guest context. A tenant's policy/licensing can deny otherwise listed
  permissions. No admin workaround or unsupported-account identity guess.
- Graph HTML conversion/sanitization may differ from MIME or change representation;
  body-only/unsupported attachments and strict timestamp precision deliberately
  limit acceptance. No arbitrary email or real-airline compatibility claim.
- New valid continuation shapes beyond the reviewed safe parser profile need
  explicit handling; token contents themselves remain opaque. Cursor access requires
  the same database protection as retained mail even though OAuth tokens are absent.
- Historical fetch timestamps, authenticated header provenance and per-folder
  visibility history are not new persisted contracts. Their absence does not alter
  evidence immutability or booking authority.

These are bounded contract qualifications, not permission to begin runtime work.
No listed stop condition requires migration, unsafe retained lookup or booking
redesign under the selected profile. A stronger product guarantee would reopen
the contract rather than silently weaken frozen behavior.

## 23. Validation and outcome

Documentation-only. Existing mail/extraction/synchronization/repository/provider,
lifetime/reinstatement, resync/migration, Gmail and 5D-A framework tests:
**317 passed + 23 subtests in 18.84s**. This includes the 145 relevant regression,
130 Gmail and 42 framework cases. No new Graph tests/runtime were implemented.
A separate transient offline repository probe passed: a synthetic two-folder
composite cursor committed atomically, reopened verbatim and remained isolated
from Gmail state. It added no test file or Graph code and is not counted in pytest.
The complete suite was not rerun for documentation-only edits; the verified frozen
5D-A full baseline is 508 passed + 245 subtests.

Whitespace/diff and scope/secret/PII/artifact inspection: passed; no secrets, PII, generated artifacts or unrelated changes found.
Only this new contract and V9_IMPLEMENTATION_PLAN.md change. Source/tests, V8,
dependencies and migrations remain unchanged. No commit, push or Phase 5E-B work.

This phase establishes how provider-specific mail mechanics can terminate at the
same neutral interface. 5E-B is architecturally safe to consider within this bounded
profile after separate authorization and its explicit implementation gates; it is
not started or authorized by this documentation. Real-airline 5D-B remains separately
deferred, and neither Graph mail nor this review completes V9.

[M1]: https://learn.microsoft.com/en-us/graph/outlook-immutable-id
[M2]: https://learn.microsoft.com/en-us/graph/delta-query-messages
[M3]: https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0
[M4]: https://learn.microsoft.com/en-us/graph/delta-query-overview
[M5]: https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
[M6]: https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
[M7]: https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0
[M8]: https://learn.microsoft.com/en-us/graph/api/resources/mailfolder?view=graph-rest-1.0
[M9]: https://learn.microsoft.com/en-us/graph/api/user-list-mailfolders?view=graph-rest-1.0
[M10]: https://learn.microsoft.com/en-us/graph/api/mailfolder-list-childfolders?view=graph-rest-1.0
[M11]: https://learn.microsoft.com/en-us/graph/api/message-list-attachments?view=graph-rest-1.0
[M12]: https://learn.microsoft.com/en-us/graph/api/resources/attachment?view=graph-rest-1.0
[M13]: https://learn.microsoft.com/en-us/graph/api/resources/fileattachment?view=graph-rest-1.0
[M14]: https://learn.microsoft.com/en-us/graph/api/resources/itemattachment?view=graph-rest-1.0
[M15]: https://learn.microsoft.com/en-us/graph/api/resources/itembody?view=graph-rest-1.0
[M16]: https://learn.microsoft.com/en-us/graph/permissions-reference
[M17]: https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc
[M18]: https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
[M19]: https://learn.microsoft.com/en-us/graph/api/user-get?view=graph-rest-1.0
[M20]: https://learn.microsoft.com/en-us/graph/errors
[M21]: https://learn.microsoft.com/en-us/graph/throttling
[M22]: https://learn.microsoft.com/en-us/graph/paging
[M23]: https://learn.microsoft.com/en-us/graph/api/message-move?view=graph-rest-1.0
[M24]: https://learn.microsoft.com/en-us/graph/api/message-copy?view=graph-rest-1.0
[M25]: https://learn.microsoft.com/en-us/graph/outlook-create-send-messages
[M26]: https://learn.microsoft.com/en-us/graph/api/mailboxitem-delta?view=graph-rest-1.0
[M27]: https://learn.microsoft.com/en-us/powershell/module/microsoft.graph.mail/update-mgusermessage?view=graph-powershell-1.0
