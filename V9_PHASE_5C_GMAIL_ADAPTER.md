# V9 Phase 5C: bounded Gmail adapter

Review date: **2026-09-11**. Branch: `feature/v9-live-replanning`.
Starting/pre-freeze HEAD: `596e76559f91460d33cf69c46783fada71fac3e1`.
Phase 5B is frozen and committed. This document accompanies the user-authorized
Phase 5C freeze commit, whose parent is that Phase 5B commit.
Architectural review conclusion after the approved response-byte correction and
validation: **A. PHASE 5C APPROVED FOR COMMIT**. The original defect and its
resolution are recorded below. The retained-identity clarification is unchanged.
No live Gmail account or mailbox was used for validation.

## Architecture and authority

Gmail API -> GmailHttpClient -> GmailMailSource -> existing MailMessage /
MailSyncPage / ProviderResult -> MailSynchronization -> BookingRepository ->
immutable evidence / deterministic extraction / canonical projection.

The new source does not import persistence, extraction, booking, itinerary, or
planning code. It performs retrieval, MIME normalization, admission filtering and
mailbox visibility only. Existing MailSynchronization remains responsible for
assembling and committing evidence, projection, visibility and cursor atomically.
No provider I/O is performed in that transaction. No V8 activation is wired.

MAIL REMOVAL != FLIGHT CANCELLATION. No model SDK, airline parsing, Graph, calendar,
flight/route provider, MCP expansion, OAuth UI, worker or scheduler is introduced.

## Approved retained-identity clarification

"Gmail tells us what changed. The local evidence store tells us what we already
retained."

This is a provider-neutral synchronization-state dependency, usable by Gmail now
and Microsoft Graph Mail later, not Gmail-specific travel logic. It answers only:
"Was this stable provider message identity previously admitted into retained mail
evidence?" Identity is exactly (provider, account_id, message_id), never version.
It does not answer booking classification, flight activity/cancellation, evidence
authority, booking supersession, or segment planning readiness.

The initial review stopped because history cannot identify local evidence admission.
For example, a later full resync can advance its lower bound beyond an old retained
message. Following restart, a label event for that message is indistinguishable in
Gmail from an event for an old message this application never retained. Gmail knows
mailbox changes, not which evidence the local synchronization transaction committed.
The user approved the narrow provider-neutral clarification before implementation.
Phase 5B's document and evidence/version contract remain unchanged.

Exact port, defined in `travel_agent/live/providers.py`:

```python
class RetainedMailIdentityLookup(Protocol):
    def __call__(self, *, provider: str, account_id: str, message_id: str) -> bool: ...
```

Truth comes from `LiveRepository.has_retained_mail_identity`, inherited by
BookingRepository: an indexed `SELECT 1 ... LIMIT 1` against existing immutable
`mail_messages`, keyed by provider/account/message only. Any retained version proves
membership; version changes do not change identity. Visibility/removal and travel
classification are irrelevant. No new table, migration or cached identity ledger.

Compose the bound read-only callable, not the repository, into the source. The
source uses it for incremental messages whose receipt predates the cursor's fixed
lower bound: true permits normalization; false prevents new admission. In-scope
messages remain eligible without a lookup. Full scans still enforce their declared
lookback locally. Permanent removals always retain their source IDs.

Lookup exceptions or non-boolean results fail the attempt; they never mean absent.
No source-local seen cache assumes that an emitted page committed. Reconstructing
both repository and provider yields the same membership after restart. Membership
reads do not open a long database transaction. A concurrent successful sync changes
the expected state and the existing repository commit guard rejects the stale batch.
The normal composition uses the same BookingRepository as MailSynchronization;
mutating mail through a separate Phase 3 writer during sync is outside this boundary.
This dependency provides synchronization state only, not travel meaning or authority.

## Client and credential injection

`GmailReadClient.read(resource, *, params, timeout, budget=None) -> dict` is the
small vendor-specific client port. Synchronization supplies one GmailByteBudget
for every read in that attempt. Clients must honor its received-byte limits before
JSON decoding; the source receives only parsed dictionaries. Fake clients fabricate
explicit byte streams and use the same bounded reader without network or credentials.
`GmailHttpClient` is the concrete standard-library HTTPS implementation; no dependency
was added. It constructs GET requests only against the fixed Gmail users/me API root,
allows only profile/history/message/attachment read paths, and refuses redirects.

Runtime injects an access-token callable. The required scope constant is
`https://www.googleapis.com/auth/gmail.readonly`. Runtime owns obtaining/refreshing
that credential, confirming account binding and the actual granted scope, consent,
restricted-scope requirements, and secure secret storage. This implementation does
not introspect tokens, establish OAuth consent, silently widen scopes, cache tokens,
or persist/log credentials. The configured stable account ID must be bound to the
authorized mailbox by runtime; `me` and recipients do not establish that identity.

Composition outline (runtime values deliberately omitted):

```python
client = GmailHttpClient(runtime_access_token)
source = GmailMailSource(
    account_id=configured_account_id,
    client=client,
    retained_identity=repository.has_retained_mail_identity,
)
outcome = synchronization.run(source, "GMAIL", configured_account_id, as_of=aware_now)
```

Here `repository` is the synchronization application's BookingRepository. The source
only calls the injected membership capability; it cannot query SQLite or projections.

## Full synchronization

The existing SynchronizationPolicy supplies the configurable 365-day default.
Read profile H0, list with epoch-second `after:` rounded down plus one second of
overlap, exclude drafts, and include spam/trash. Exhaust list pagination even on
empty pages; ignore resultSizeEstimate. Deduplicate and sort message IDs, fetch FULL,
and enforce receipt >= since at millisecond precision. No sender/travel-keyword
filter or snippet extraction. Drafts and out-of-scope overlap results are excluded.

Exhaust history from H0; require no changes and terminal checkpoint H0, then require
the final profile checkpoint H0. Any detected mutation fails retryable UNAVAILABLE.
Missing/disappearing gets fail NOT_FOUND, not a successful partial scan. Missing or
expired bootstrap checkpoints cannot generate synthetic cursors. A quiet empty
mailbox result still requires valid history validation and a final profile check.

This implements Phase 5B's bounded design inference, not a transactional snapshot
claim. Busy mailboxes may repeatedly fail. Cross-endpoint consistency remains an
explicit uncertainty, and no live activation relies on this scan.

## Incremental synchronization and cursor

The private versioned JSON cursor binds format, account, discovery profile, initial
lower bound and checkpoint. Invalid/mismatched envelopes return CURSOR_EXPIRED.
The rolling `since` argument does not narrow an existing delta's fixed lower bound.
Runtime must request explicit full synchronization when changing discovery policy.

Exhaust history pages using the same startHistoryId, without label/type filters.
Use typed events; generic duplicate IDs are not additional work. Validate increasing
record/checkpoint ordering, token cycles, duplicate-record equivalence and conflicting
same-record events. Coalesce affected IDs deterministically before fetching FULL.
A terminal permanent deletion emits only a removal. Contradictory terminal evidence
fails INVALID_RESPONSE; an uncovered message disappearance fails NOT_FOUND.

After all selected bodies are fetched, require profile checkpoint == terminal history
response checkpoint. Emit exactly one bounded core page, with the completion cursor
only after validation. Vendor pages need not map to core pages. No continuation token
or partial in-memory resume is supported; a non-null page_token fails explicitly.
An empty valid delta has empty evidence/removal tuples and a terminal cursor.

CURSOR_EXPIRED flows through the existing persistent FULL_RESYNC_REQUIRED mechanism.
Incremental attempts cannot clear it, failed full attempts preserve it, and only a
complete successful full synchronization transaction clears it. There is no alternate
recovery mechanism or implicit full fallback.

## MIME and evidence versions

The FULL profile walks multipart mixed/alternative/related in document order and
accepts at most one non-attachment text/plain and one text/html leaf, retaining both.
Non-body attachments are excluded. Unsupported structures, repeated body types,
nested message/rfc822, encrypted content and attachment-only mail fail explicitly.
No body fragment is emitted as a complete message.

Header names are case-insensitive; valid CRLF folding is unfolded, encoded words are
strictly decoded, conflicting relevant duplicates and invalid addresses fail.
Repeated irrelevant transport headers such as Received are ignored. One From
mailbox is required; absent Subject becomes empty; To/Cc become sorted unique
normalized addresses. Address groups are conservatively unsupported as malformed
for this initial profile. No inferred Bcc or authenticated-sender claim.

Body data uses strict canonical base64url decoding with exact decoded-size checks.
Selected attachment-backed text is fetched and checked, never treated as empty.
Accepted charsets: UTF-8, US-ASCII, ISO-8859-1, Windows-1252. Missing charset requires
ASCII. Transfer encoding is limited to absent/7bit/8bit/binary; other declarations
fail UNSUPPORTED_CAPABILITY, with no guessed second transfer decode. HTML preserves
decoded evidence with CRLF/CR -> LF; existing MailMessage text normalization is reused.

Receipt is derived exactly from integer internalDate milliseconds in UTC. Provenance
is fixed to `gmail-api/internal-date/mail-normalization-v1`, observed_at = receipt,
retrieved_by = PROVIDER; thread_id = None. A provisional local MailMessage is replaced
with the existing `mail_evidence_version()` result before emission. Versions remain
`mail-evidence/v1:sha256:<digest>`. Labels, history IDs, cursors, snippets and retrieval
times never enter the fingerprint. No existing serialization or hash rule changed.

## Bounds and errors

Default limits: 100 list pages, 100 history pages, 20,000 requests, 10,000 items/typed
events, 8 MB per JSON response, 32 MB cumulative response bytes, 2 MB decoded selected
MIME bytes per message, 100 parts, depth 12, 120-second attempt deadline and 15-second
request timeout. JSON response bytes bound input including non-body attachments.
Exceeding size/count bounds fails UNSUPPORTED_CAPABILITY; deadline failure is
TIMEOUT. The received-byte limits are enforced before JSON parsing by the client,
per response and cumulatively across one synchronization attempt. They do not bound
the repository's lifetime retained history. The effective response ceiling is the
smaller of the configured source and HTTP-client response limits. Exact semantics,
including the single rejected overflow-probe byte, are recorded below.

The source checks elapsed time before/after I/O and before successful emission.
The HTTP client also checks deadlines between bounded body reads and supplies socket
timeouts. These are cooperative deadlines: operating-system DNS resolution or an
in-progress socket operation is not forcibly cancelled at the exact attempt deadline.
Injected clients must honor the timeout argument; overruns cannot produce successful
source output. No retry loop is embedded: one attempt is the retry bound, with safe
retryability/delay for a later caller-controlled attempt.

401 -> AUTH_REQUIRED; permission/domain 403 -> PERMISSION_DENIED; quota 403/429 ->
RATE_LIMITED (daily quota is not immediately retryable); 500/502/503/504 -> retryable
UNAVAILABLE; timeout -> TIMEOUT; transient connection/DNS -> retryable UNAVAILABLE;
TLS validation failure -> nonretryable UNAVAILABLE; message/attachment 404 -> NOT_FOUND;
history 404 -> CURSOR_EXPIRED; malformed/ambiguous JSON, bad request or unexpected
exception -> INVALID_RESPONSE; unsupported MIME/limits -> UNSUPPORTED_CAPABILITY.
Numeric Retry-After is bounded to 3600 seconds; unsafe/unparsed delays are omitted.
ProviderResult contains only safe codes/retry flags/delays, not provider exceptions,
URLs, response bodies or credentials. Freshness/staleness remains a core decision.

## Validation and limitations

Tests use fabricated responses and the existing synthetic Northstar fixtures only.
No new airline parser or real mailbox fixture. Socket access is forbidden in the
new tests; HTTP tests stub the opener while testing real request/error mechanics.
Coverage includes approved membership cases A-F, version/restart replay, pagination,
quiet-window and disappearing-message races, empty deltas, terminal deletion,
archive/trash labels, MIME alternatives/charset/base64url/header errors, attachment
bodies, resource limits, safe errors, stale commits, extraction/projection rollback,
and persistent recovery. Exact run results are recorded in the continuation plan.

No migration, immutable evidence change, booking identity/lifetime/reinstatement
change, V8 source/test behavior change, model dependency, or new runtime dependency.
Sustained ingestion still needs review of all-history reprojection cost. Arbitrary
MIME/airline coverage, quarantine, authenticated document trust, production OAuth,
continuous monitoring and completed V9 integration are not claimed.

The Phase 5C architectural blocker is resolved. Phase 5D is ready for separately
scoped review/authorization; real airline extraction templates and document authority
remain unimplemented. Approval for commit does not authorize Phase 5D or live use.
No Phase 5D work or push was performed. The subsequent freeze instruction authorizes
the local Phase 5C commit only.

## Complete architectural review decision (2026-09-11)

**A. PHASE 5C APPROVED FOR COMMIT**, after correcting the sole blocking finding and
running the complete validation below. Review covered the frozen Phase 5B contract, the approved
retained-identity clarification, this document, the continuation plan, all three
Gmail modules, both new test files, and the existing synchronization/repository,
migration and evidence-version boundaries. Passing tests alone are not approval.

| Check | Review result |
|---|---|
| A. Retrieval/normalization only | Pass: client/source/MIME modules supply generic evidence and visibility; existing service owns the atomic commit. |
| B. No travel reasoning | Pass: no classification, authority, booking or planning decisions in GmailMailSource. |
| C. Full lookback | Pass: fixed since, overlapping epoch query, local millisecond cutoff, drafts excluded, spam/trash included, quiet-window validation. |
| D. Incremental history | Pass: fixed start across pages, increasing checkpoints/records, cycle/conflict rejection, typed-event deduplication and deterministic final ordering. |
| E. Terminal checkpoint | Pass: checkpoint selection/race checks preserved; received-byte overflow prevents both completion emission and commit. |
| F. Persistent recovery | Pass: CURSOR_EXPIRED uses existing persistent recovery; only a successful complete full transaction clears it. |
| G. Bounded fail-closed normalization | Pass after correction: actual received bytes are bounded before parsing, per response and attempt; MIME body/part/depth/charset/base64url semantics are unchanged. |
| H. Evidence fingerprint | Pass: existing mail_evidence_version helper and frozen normalization profile; no hash/serializer changes. |
| I. Removal | Pass: source emits removal IDs; core records visibility without creating travel cancellation. |
| J. Credentials | Pass within runtime boundary: GET-only fixed HTTPS root, no redirects, injected token, readonly scope requirement; actual grant/account binding remain runtime duties. |
| K. Neutral errors | Pass: safe category/retryability/delay mapping; no raw provider exceptions, bodies or credentials in ProviderResult. |
| L. Persistence | Pass: generic indexed immutable-evidence lookup; no Gmail persistence abstraction, table, migration or identity cache. |
| M. Phase isolation | Pass: no airline templates, Graph, calendar, FlightAware, Routes, workers, schedulers, HITL or MCP implementation added. |

### Original blocking finding (resolved): actual response bytes were lost at the client/source boundary

Before correction, `GmailHttpClient.read` limited each response, then returned only
a parsed dict. `_Attempt.read` computed `len(json.dumps(result).encode("utf-8"))`.
JSON whitespace and escaped spellings need not survive parsing/reserialization,
so this is not the number of bytes received. It can undercount or overcount.
The source's per-response setting also measured that representation rather than
the transport bytes; the client's independent ceiling did not fix batch totals.

Deterministic offline reproduction with the real HTTP client and a stubbed opener:
configure both per-response ceilings to 100 bytes and the attempt batch ceiling to
100 bytes. Return four valid JSON bodies, each right-padded with spaces to 90 bytes:
profile {"historyId":"10"}, messages {"messages":[]}, history {"historyId":"10"},
and final profile {"historyId":"10"}. The old attempt received **360 bytes** but
counted only **73 reserialized bytes**, succeeded and emitted a completion cursor.
Every individual body was within its ceiling. No network or real credentials were used.

### Approved correction and exact received-byte semantics

Implemented only the approved correction. `GmailByteBudget`, in gmail_client.py,
is the single attempt-local state holder and bounded body reader. It is required
to share a remaining allowance across reads without mutable counters on a shared
HTTP client. `_Attempt` constructs it from GmailLimits and passes it to every
GmailReadClient.read call. GmailMailSource neither reads HTTP bytes nor estimates
them from dictionaries. No provider-neutral port, error category, repository or
evidence contract changed. The Gmail-specific read port gains only the optional
budget keyword; a standalone client.read call creates its own one-response budget.

The flow is HTTP body -> budget.read_body -> UTF-8/JSON decode and validation ->
parsed dict -> source. The source's json.dumps size-estimation path is removed.

- **Per response:** default 8,000,000 response-body bytes, inclusive. The effective
  ceiling is min(GmailLimits.max_response_bytes, GmailHttpClient's configured cap).
- **Per attempt:** default 32,000,000 bytes, inclusive, accumulated across profile,
  every list/history page, message bodies, selected attachments, and HTTP error-body
  bytes actually consumed during that source.sync call. A later attempt always gets
  a fresh object, after either success or failure, even with the same client/source.
- Count len(chunk) for bytes delivered by the HTTP response's read1, including JSON
  whitespace, UTF-8 bytes and literal escape sequences. These are response-body
  bytes, excluding HTTP headers, transfer framing and TLS overhead. No semantic JSON
  reserialization or Unicode-character count is used as transport measurement.
- Before each read, cap its requested size to at most 65,536 bytes and remaining
  response/batch allowance plus one. The one-byte probe is necessary to distinguish
  EOF exactly at an inclusive limit from an oversized body without trusting length
  headers. If nonempty, it is counted as consumed and rejected immediately before
  appending the over-limit chunk or parsing JSON. Thus an overflow attempt may
  consume at most one byte beyond the allowance; that byte is never accepted as
  evidence. Reading stops and the response is closed on failure.
- HTTP-error diagnostics retain the prior 65,536-byte parsing cap, with one byte
  to detect a longer diagnostic body. All consumed diagnostic bytes count against
  the same response/attempt budgets. A body beyond the diagnostic cap alone is not
  parsed; existing status mapping is retained. Actual configured-budget exhaustion
  or a checked deadline is propagated, not swallowed by best-effort error parsing.
- Exhaustion raises GmailFailure(UNSUPPORTED_CAPABILITY, retryable=False), translated
  by the source to ProviderResult with value=None and a safe ProviderError. No new
  public error enum or transport exception escapes, no cursor is emitted, and the
  existing service records failure without committing evidence or advancing cursor.
- Socket timeout and attempt deadline arguments are preserved. Deadline checks run
  before and after bounded body reads, including error-body reads; source checks,
  request/page/item limits and MIME limits remain in force.

The reproduced 100-byte attempt now reads 90 bytes from the first response and at
most 11 from the second (10 remaining plus one rejected probe), then fails without
parsing the oversized response or requesting the remaining pages. The same source
and client can immediately complete later independent small-response attempts.

### New deterministic byte-budget regression coverage

22 parametrized cases in tests/test_v9_gmail_client.py (test_received_bytes_*):

| Requirements | Coverage |
|---|---|
| A, F, G | Padded and escaped JSON exceeding the source's configured limit is rejected before JSON parsing or cursor emission. |
| B, C | Exact per-response and whole-attempt boundaries succeed; one byte over fails. |
| D | Raw bytes accumulate through multi-page listing and message/attachment retrieval, including profile/history reads. HTTP error bodies share the attempt budget. |
| E | Same source/client succeeds after a failed attempt and after a successful attempt; standalone reads also reset. |
| F | UTF-8, Unicode escape case, whitespace and formatting variants use actual wire lengths, despite equivalent parsed values. |
| G | Observed stream stops after one rejected probe; repository integration preserves the prior cursor/evidence/run count on exhaustion. |
| H, J | Existing successful flows and all original 108 focused cases remain green. |
| I | Timeout before/after received-body reads is preserved for success and HTTP-error responses; existing timeout tests remain green. |

The existing fake client now encodes its fabricated responses into explicit BytesIO
streams and honors the same budget before parsing. This is synthetic test transport,
not production estimation of provider bytes. No airline or real mailbox fixtures.

### Required retained-identity tests confirmed

| Requirement | Existing deterministic coverage in tests/test_v9_gmail.py |
|---|---|
| Retained old message remains processable | test_old_delta_membership_distinguishes_admission (True case) |
| Never-retained old message excluded | Same parametrized test (False case), plus persisted restart test |
| New in-lookback discovery | test_in_scope_new_admission_does_not_need_membership (full and incremental) |
| Version changes preserve identity membership | test_restart_persisted_membership_versions_and_scope retains two versions under one message identity |
| Restart uses persisted membership | Same test closes/reconstructs repository and source after full lookback moves beyond retained evidence |
| Source has no persistence/travel dependency | test_source_imports_no_persistence_or_travel_modules, confirmed by direct source/import review |

Additional tests cover lookup exceptions, provider/account isolation, removal with
retained membership, stale commits and projection rollback. No additional tests
were necessary for the six approved clarification cases.

Pre-correction review baseline: focused Phase 5C **108 passed in 2.34s**; relevant synchronization,
repository, recovery, projection, lifetime, reinstatement, extraction, provider and
mail-contract tests **145 passed + 23 subtests in 11.33s**; complete suite
**444 passed + 245 subtests in 49.67s**, no failures/skips. The separate byte-budget
reproduction demonstrates a coverage gap, not a passing conformance test.
That earlier review changed documentation only and stopped at the blocker. The
subsequent user-authorized correction changed gmail_client.py, gmail.py, both
Gmail test files and the two Phase 5C/continuation documents. No dependencies added.

Post-correction validation: new byte-budget regressions **22 passed in 3.00s**;
all Phase 5C focused tests **130 passed in 3.68s** (original 108 plus 22 new);
relevant synchronization/repository tests **145 passed + 23 subtests in 9.91s**;
complete suite **466 passed + 245 subtests in 34.86s**, no failures or skips.
Hashes confirm that retained-identity ports/lookup, MIME normalization, mail evidence,
synchronization, booking repository, migrations, frozen Phase 5B contract and both
dependency manifests are unchanged by the correction. Existing V8 source/tests are
unchanged against v8. No migration, dependency or phase-scope expansion occurred.
Tracked and untracked diff/whitespace checks passed. Inspection and secret-pattern
scanning of all nine changed/untracked files found no secrets, generated artifacts
or unrelated changes in the proposed change set. At the pre-freeze review, the
staged diff was empty and HEAD was 596e76559f91460d33cf69c46783fada71fac3e1;
v8 and v7 still resolve to their
frozen commits. Existing V8 source/tests are byte-unchanged against v8. These checks
do not validate live Gmail behavior. The reproduced resource-budget blocker is now
resolved and regression-tested. The user subsequently authorized a local commit
with message `V9 Phase 5C: add Gmail provider adapter and bounded synchronization`.
Final freeze review confirmed the same nine-file scope and test baseline, with no
source/test edits. Phase 5D requires a separate scoped instruction and has not begun;
this freeze does not authorize a push.

## Official Google references

Reviewed 2026-09-11. Public documentation only, not live mailbox verification.
The freeze distinguishes documented facts from conservative design inferences.

- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
- https://developers.google.com/workspace/gmail/api/guides/sync
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/Format
- https://developers.google.com/workspace/gmail/api/guides/filtering
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments
- https://developers.google.com/workspace/gmail/api/guides/labels
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/workspace/gmail/api/guides/drafts
- https://developers.google.com/workspace/gmail/api/guides/handle-errors
