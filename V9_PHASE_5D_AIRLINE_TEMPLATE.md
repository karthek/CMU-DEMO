# V9 Phase 5D-A: multi-airline deterministic extraction framework

**Phase 5D-A: APPROVED FOR FREEZE.** The user approved the generic framework and
its explicit version/event authority boundary on 2026-09-12.
**Phase 5D-B: DEFERRED** until sanitized, format-faithful actual airline email
evidence is supplied. The absence of that evidence does not block the 5D-A freeze.

This record accompanies the approved Phase 5D-A commit on
`feature/v9-live-replanning`, with parent
`5584fb472e9c2d3e15302718820038fba60afd7b`. The existing uncommitted framework,
tests, fixtures and two Phase 5D documents were preserved and reviewed before
freezing. Phase 5C remains frozen. No push, Phase 5D-B or Phase 5E work is included.
The historical single-family review below records the earlier blocker; the user's
5D-A/5D-B split supersedes its phase-wide blocked status.

Real-airline support is intentionally unclaimed until a sanitized, format-faithful
message from the target template family is available for deterministic validation.

## 1. Resolved architectural blocker

The frozen `booking_event` had a `synthetic-northstar/v1` branch for interpreting
ordering/reinstatement evidence. Other eligible rules could still create
unsequenced events, including isolated booking or cancellation events. Merely
adding another parser therefore neither granted safe ordered mutations nor denied
unknown mutation reliably. A parser registry alone could not solve this.

The new boundary separates two meanings of authority:

- **Template permission:** an exact reviewed rule version may emit specified event
  types. This is immutable deployment configuration, not a fact supplied by mail.
- **Document order:** existing `EventAuthority(lifetime, sequence)` establishes
  ordering within the frozen lifetime rules. Permission never manufactures this
  evidence or allows a later email to reinstate cancelled travel.

A parser determines what evidence says. An authority policy determines whether
that exact parser/version is trusted to mutate canonical travel state.

## 2. Exact authority API and ownership

`TemplateEventGrant(rule_id, carrier, event_types: frozenset[ExtractionState],
event_builder: Callable[[ExtractionResult], BookingEvent])` is frozen.
`TemplateEventAuthorityPolicy(policy_id, grants: tuple[TemplateEventGrant, ...])`
is frozen, rejects duplicate/unversioned IDs, and sorts grants deterministically.
There is no add/remove/grant API, provider setting, database trust table, remote
configuration loader or mail-controlled policy mutation.

`policy.decision(result)` returns `TemplateAuthorityDecision(policy_id, rule_id,
event_type, permitted, reason)`. Reasons are `AUTHORIZED`, `NOT_ELIGIBLE`,
`EXTRACTION_ISSUES`, `UNREGISTERED_TEMPLATE`, `EVENT_NOT_AUTHORIZED`, and
`CARRIER_MISMATCH`. `policy.construct(result)` returns an event only if permitted;
it checks that the trusted interpreter preserves extracted facts and event type.
An interpreter contract violation raises `ValueError`, allowing the existing sync
transaction/failure boundary to fail closed without partial canonical state.

`booking_event(result, *, policy=None)` delegates to this one policy boundary.
The default `template-authority/v1` explicitly grants only
`synthetic-northstar/v1` / NS / BOOKING, CHANGE, CANCELLATION. Its old document
interpreter is moved unchanged to `template_events.northstar_event_v1`.
Six frozen-HEAD event serialization hashes verify byte/ID compatibility, including
unsequenced, invalid-order and reinstatement evidence.

Parser registration grants **no** event permission. Version v1 never authorizes
v2. A BOOKING+CHANGE grant never authorizes CANCELLATION. Airline name, sender,
provider, PNR, successful parsing and classification do not create permission.
The grant's carrier is an additional constraint, never an independent grant.

`BookingRepository(..., event_policy=DEFAULT_TEMPLATE_EVENT_POLICY)` receives this
immutable capability once at construction. `_project_all` authorizes eligible
extractions **before reconciliation creates any canonical rows**. Denied evidence
and typed extraction remain retained and queryable but create no booking, segment,
event or changed canonical revision. Unknown changes/cancellations cannot poison
or cancel an existing authorized projection. No canonical SQL enters any parser.

## 3. Registry and deterministic selection

`TemplateDefinition(rule_id, carrier, family, event_types, parser)` describes an
exact versioned family; `parser` implements existing `ExtractionRule.matches`
and `extract`. Match conditions remain explicit deterministic parser code.
`TemplateRegistry(templates: tuple[TemplateDefinition, ...])` rejects duplicate IDs
and evaluates every rule in sorted ID order. `ItineraryExtractor(registry=...)`
uses it. The existing `ItineraryExtractor(rules=...)` injection remains available;
legacy rules may omit carrier metadata, which never confers authority.

- Zero matches preserves the frozen keyword-based NOT_TRAVEL / unsupported
  UNRESOLVED distinction. Those keywords are a conservative fallback, not airline
  classification or permission.
- One match validates result type, version, original message, declared event set,
  segment type, carrier and issue structure.
- Multiple matches always yield UNRESOLVED / AMBIGUOUS_TEMPLATE, even if only one
  has authority. Registration order cannot pick a winner.
- Match exceptions/non-boolean matches and invalid parser results yield safe
  structured UNRESOLVED issues, without exposing parser exception messages.
- Missing/conflicting identity remains unresolved under each deterministic rule;
  the framework does not repair or guess fields.

The registry has no dependency on the authority policy, Gmail, Graph, SQLite,
canonical bookings or planning. Input remains normalized `MailMessage`, with the
same frozen `ExtractionResult` serialization and five classifications. No mailbox
IDs, labels, history, cursors or MIME API objects influence template interpretation.

## 4. Prior candidate review: real support remains deferred

Public airline guidance established business concepts, but did not provide a
sufficiently stable, complete, source-grounded email grammar for deterministic
parsing. Unsupported airline claims were rejected rather than fabricated from
incomplete examples. Public-page grammar searching is closed for this phase;
Phase 5D-B requires supplied format-faithful actual evidence instead.

Only public airline guidance was used in the prior candidate review. No customer mailbox,
passenger receipt, private confirmation, ticket number or token was copied. Indexed
passenger documents, generated sample tickets and illustrative schema examples
were not treated as verified airline template specifications.

| Candidate | Verified guidance | Missing evidence / decision |
|---|---|---|
| Delta Flight Receipt candidate | [Delta's change/cancel guide](https://www.delta.com/us/en/change-cancel/how-to-cancel-or-change-your-flight) identifies the inbox search phrase "Your Flight Receipt" and confirmation number location below the logo/name. | No complete versioned sender/body/segment/time grammar or safe traveler/coupon mapping established. Reject for this slice; no parser or grant. |
| American aa.com award-trip correspondence | [AA's award-travel guide](https://www.aa.com/web/i18n/aadvantage-program/answers-support/using-miles-for-travel.html) identifies a six-character code in booking mail and a new ticket number after a supported change. | No complete matched email family; reissue cannot be assumed to preserve segment/lifetime identity. Reject; no parser or grant. |
| United reservation/e-ticket correspondence candidate | The official [Find a Trip page](https://www.united.com/en/EC/manageres/mytrips) describes trip lookup, not a deterministic email specification; the US page rendered no useful template content during review. | No safely verified sender, subject, full body or event/identity grammar. Reject; no parser or grant. |

These are findings about the evidence reviewed, not claims that those airlines
cannot be parsed. A website domain is not a validated sending address. An inbox
search phrase is not an exact subject grammar. A receipt-request address is not
a sender allowlist. No inferred sender, date/timezone, traveler token or coupon
reference was installed.

For all three candidates the exact supported sender/subject/body/flight/date/time/
airport/traveler/segment characteristics and authorized real event sets are
**none**. No real fixtures or real-template tests exist. BOOKING-only support would
be acceptable if an exact family and required identity mapping were grounded;
missing sequence evidence is not by itself a requirement to fabricate ordering.
CHANGE/CANCELLATION/reissue would need additional explicit safe association.

## 5. Bounded implemented support and demonstration

Default production registry/policy still contain only fictional
`synthetic-northstar/v1`: BOOKING, CHANGE, CANCELLATION. Existing sender, subjects,
labeled fields, HTML normalization, traveler/coupon references and single-segment
shape are unchanged. This is not real-airline support.

Tests add **fictional** `fixture-alpha/v1`, `fixture-beta/v1`, `fixture-gamma/v1`
with distinct XA/XB/XC carriers, reserved `.example.test` senders, exact family/event
subjects and explicit body markers. They reuse the synthetic Northstar field grammar
and document interpreter under explicit test-only grants. They are never added to
the default registry/policy and are not Delta/American/United format substitutes.

Their flow is normalized MailMessage -> registry -> typed extraction -> exact event
permission -> reconciliation -> event/evidence links -> BookingRepository projection
-> planning readiness. Tests demonstrate booking readiness, changed schedule with
stable identity/history, explicit cancellation denying readiness, duplicate replay,
restart, and a later booking failing to reinstate cancelled travel. Three distinct
family registrations require no edits to booking/planning architecture.

## 6. Frozen safeguards and provenance

The projector, comparators, booking lifetime checks, reinstatement assertions and
planning-readiness code remain unchanged. Reconciliation still requires explicit
carrier + PNR + authorized traveler reference + stable segment reference. Same PNR
is not proof of the same lifetime. A new ticket is not assumed to be the same coupon.
No supersession, refund, rebooking or operational flight status is inferred.
MAIL REMOVAL != FLIGHT CANCELLATION.

Immutable retained extraction already records source provider/account/message/
evidence version, selected rule ID, classification, structured segment and issues.
Existing event/evidence and revision/event links explain canonical outcomes.
`BookingRepository.template_authority_decisions()` adds a read-only explanation
for every retained extraction under the current immutable deployment policy.

This query is **current evaluation**, not a new persisted historical policy ledger
or revocation mechanism. Old committed events remain authoritative under frozen
persistence semantics. A differently configured deployment can reconsider previously
denied retained extraction on a later projection; changing an existing event's
interpretation is still rejected by immutable event/evidence checks. Policy/parser
composition is reviewed application code and exact IDs must not be reused with
changed semantics. No automatic reprocessing, runtime trust administration or
historical policy migration is introduced. No extra raw content is persisted.

## 7. Validation

Freeze validation was rerun on 2026-09-12; results are recorded below. Tests are deterministic/offline;
no mailbox credentials or live provider API calls were used.

- Framework: 42 passed (authority selection: 26; registry selection: 20; the two
  selections overlap in 4 cases).
- Authority tests: unknown BOOKING/CHANGE/CANCELLATION, denied updates preserving
  current state, exact version isolation, partial grants, immutable configuration,
  event fact preservation, explicit grants, provider independence, replay/restart.
- Registry tests: three families, every order permutation, cross-family boundary,
  unknown layouts, conflict/missing/malformed fields, ambiguity, duplicate IDs,
  parser failures and forged version/source/carrier rejection.
- Real-template focused tests: not applicable; no real family implemented.
- Existing extraction/reconciliation/synchronization/repository/lifetime/reinstatement/
  migration/resync/provider contracts: 145 passed + 23 subtests.
- Frozen Phase 5C Gmail: 130 passed.
- Complete suite: 508 passed + 245 subtests in 78.21s; no failures or skips.
- Whitespace/diff, secret/PII/generated-artifact/scope review: passed; no PII, secrets, generated artifacts or unrelated changes found; V8 source/tests unchanged.

Fixture added: `tests/fixtures/v9_templates/northstar_event_hashes.json`, six hashes
captured from the frozen HEAD implementation, not recomputed expected values from
the new interpreter. Fictional family messages are constructed in the framework
test module from existing sanitized synthetic fixtures.

## 8. Adding a future reviewed family

1. Establish safe source-grounded sender, subject, body, identity, time and event
   semantics. Reject unsupported variants/reissue instead of broadening the model.
2. Implement a deterministic `ExtractionRule` with a new exact versioned ID and
   explicit metadata; add sanitized format-faithful fixtures and failure tests.
3. Register a `TemplateDefinition` in the application composition. Parsing alone
   must continue to have no canonical mutation permission.
4. Review a versioned event interpreter that preserves extracted facts and supplies
   only explicit document/lifetime/transition evidence. Add a separate immutable
   `TemplateEventGrant` for only the supported event types.
5. Inject the registry into the extractor and the policy into BookingRepository;
   prove end-to-end replay, provenance and safety with existing tests.

No Gmail/Graph adapter, repository internals, planner, Beam Search, Critic, MCP or
ReplanningEvaluator change is required when adding another supported template.
A new version must get its own review and grant; no prefix/domain inheritance.

## 9. Approved capstone claim and freeze boundary

> The itinerary extraction layer is airline-agnostic at its core. Airline-specific
> formats plug into a versioned deterministic template registry and require explicit
> event authority. The framework is validated with deterministic test families;
> real-airline compatibility is claimed only after format-faithful evidence is
> validated.

Phase 5D-A freezes the reusable framework, not a universal airline parser or
production extraction coverage. Delta, American and United are not currently
supported. The architecture remains multi-airline; deferred real-family validation
does not narrow the registry or authority model to Northstar.

No dependencies, migrations, V7/V8 changes, Graph/calendar/FlightAware/Routes,
worker/scheduler, HITL/MCP, new travel domains, live replanning or autonomous
transactions are included. Phase 5D-B and Phase 5E have not begun. No push.

## 10. Phase 5D-B entry criteria: supplied actual evidence

A future Phase 5D-B may begin when one or more sanitized actual airline messages
are supplied. Suitable evidence may derive from a user-owned booking confirmation,
change notification or cancellation notification. It must preserve the target
family's structural format; a synthetic reconstruction must never be called real
evidence.

Sanitize traveler names, confirmation/PNR values, ticket numbers, loyalty numbers,
email addresses, payment/card data, postal addresses, phone numbers, QR/barcode
values and every other personal identifier. Replace sensitive values consistently
where cross-message relationships must be tested, retaining format-critical value
shapes without retaining actual identifiers.

Preserve sender domain, subject pattern, section headings, field labels, ordering,
required HTML/text structure, flight/date/airport field placement and explicit
booking/change/cancellation markers. Sanitizing an email address must preserve the
sender domain needed for template validation while removing personal address data.
Do not invent missing content, identity continuity, event order or reinstatement.

The supplied evidence allows a bounded family to be assessed, not automatically
trusted. For each supported event type, establish exact deterministic parsing and
safe identity mappings under the frozen model, add sanitized fixtures and focused
failure/E2E tests, register the exact template version and obtain a separately
reviewed authority grant. BOOKING evidence alone does not establish CHANGE or
CANCELLATION support. Reissue/lifetime ambiguity remains unsupported unless the
existing safeguards can represent explicit evidence safely.

Until that evidence is supplied and validated, real-airline compatibility remains
unclaimed. No real-family parser, grant, fixture or live mailbox work is added by
this freeze, and Phase 5D-B is not started.

---

## Historical single-family review (superseded scope)

The following is the preserved pre-framework record. Its implementation/status/test
statements describe that earlier documentation-only attempt, not the current diff.
The synthetic-only authority finding remains valid history and is addressed above.

### Previous review: real airline template eligibility

Decision: **B. PHASE 5D BLOCKED** before parser implementation.
Review date: 2026-09-11. Repository:
`C:\Users\Karth\OneDrive\Desktop\CMU\CMU Capstone\model_agnostic_travel_agent_v1`.
Starting branch: `feature/v9-live-replanning`; starting HEAD:
`5584fb472e9c2d3e15302718820038fba60afd7b`. The worktree was clean.
Phase 5C remains frozen. Only this review and the implementation-plan checkpoint
are changed. No production code, tests, fixtures, dependencies or migrations added.

## 1. Airline selection and evidence boundary

No real template family passed the selection gate. The one candidate workflow
reviewed was **American Airlines aa.com award-trip confirmation and associated
change/cancellation correspondence**. A single traveler and single AA-operated
nonstop segment would be the proposed first shape, not a verified supported layout.
This is a rejected candidate for this attempt, not implemented American support.

Repository inspection found only the fictional Northstar family, with seven
fixtures in `tests/fixtures/v9_mail/messages.json`; no candidate real-airline
fixture or exact real-email layout reference was present. Those labeled fields
must not be renamed to American Airlines and presented as realistic airline mail.

American's official [Using miles for travel](https://www.aa.com/web/i18n/aadvantage-program/answers-support/using-miles-for-travel.html)
page documents a confirmation code in the booking email and a new ticket number
in the email sent after an eligible award-trip change. This made it a relevant
candidate to investigate. The reviewed guidance does not supply an exact matched
booking/change/cancellation email layout, stable coupon continuity, or an explicit
booking-lifetime/monotonic-document-sequence contract. The changed ticket number
cannot simply be treated as a stable segment reference across that workflow.
This last point is a compatibility assessment, not a claim about every AA email.

No passenger documents, private mailbox access, real confirmation codes or ticket
numbers were copied into the repository. Only public official workflow guidance
informs the candidate assessment. Publicly indexed passenger receipts and unrelated
sample-ticket generators were not used as fixture sources.

## 2. Exact supported characteristics

There is **no new supported message family**. The following remain unverified,
so no matches, regexes or invented fixtures were installed:

| Characteristic | Candidate eligibility result |
|---|---|
| Sender/address/domain | Exact sending identity is not established by the reviewed reference; aa.com being the website is not a sender allowlist. |
| Subject family | No exact booking/change/cancellation subject set verified. |
| Text/HTML markers | No bounded matching set of body layouts verified. |
| Event indicators | Workflow guidance is not an exact email grammar for BOOKING, CHANGE or CANCELLATION. |
| Schedule fields | No verified family-level grammar for carrier/flight, dates, airports or local times. |
| Booking reference | Confirmation-code presence is documented; its presence alone does not establish booking lifetime or segment identity. |
| Traveler reference | No exact mapping to the configured authorized traveler reference established. Name, recipient and mailbox ownership cannot substitute. |
| Stable segment reference | Continuity through ticket changes is unproved. Flight/date/route or segment position cannot substitute. |
| Lifetime and document order | No verified mapping to EventAuthority(lifetime, sequence). Receipt/fetch time, ticket-number magnitude and email versions cannot substitute. |
| Itinerary shape | Single traveler/single segment is a proposed restriction only; multiple segments and all other layouts remain unsupported. |

## 3. Existing extraction architecture and deterministic parsing rules

The frozen route is MailMessage -> ItineraryExtractor's selected ExtractionRule ->
ExtractionResult -> exact reconcile -> booking_event -> BookingRepository's
immutable events and all-history projection -> planning_readiness.
Gmail remains retrieval/MIME infrastructure and is unchanged.

`ExtractionResult` carries state, source message, rule_id, one ExtractedSegment and
issues. It does not carry an EventAuthority. The existing rule injection point can
add deterministic field parsing, but registration alone cannot establish document
authority in the downstream event converter. There is no new parsing grammar here.
No LLM, probabilistic parsing, fuzzy completion or airline-specific MCP path is used.

## 4. Exact blocker: event construction and supported transitions

In `travel_agent/live/booking.py`, `booking_event()` recognizes authority and
reinstatement fields only for rule_id `synthetic-northstar/v1`. An extraction from
another rule yields a BookingEvent with authority=None. In
`travel_agent/live/booking_repository.py`, `_project_all()` uses that existing
converter on persisted extraction evidence; injecting a different extractor does
not replace it.

`project()` deliberately returns UNRESOLVED / CONFLICTING_EVENT_HISTORY when event
facts differ and any event lacks comparable authority. The enum can label CHANGE
or CANCELLATION, but the pipeline cannot safely apply the required transitions
merely from those labels. This is correct fail-closed behavior, not a projector bug.

An isolated synthetic diagnostic used a NorthstarRule subclass with
rule_id `test-only-contract-probe/v1`, the existing fabricated Phase 4 fixtures,
an injected ItineraryExtractor, OfflineMailSource, MailSynchronization and a fresh
temporary BookingRepository for each scenario. Even the synthetic fixtures' explicit
lifetime/sequence labels confer no authority under this different rule identity.
The diagnostic is not an airline parser or real-airline fixture.

| Synthetic diagnostic | Result through actual repository pipeline |
|---|---|
| First BOOKING with complete existing identity/schedule | BOOKED; booking-only planning readiness allowed. |
| That BOOKING followed by CHANGE | Typed CHANGE; authority=None; current segment UNRESOLVED with CONFLICTING_EVENT_HISTORY; planning denied; both evidence links retained. |
| That BOOKING followed by CANCELLATION | Typed CANCELLATION; authority=None; current segment UNRESOLVED with CONFLICTING_EVENT_HISTORY; planning denied; both evidence links retained. |

Both diagnostic scenarios passed their assertions. A lone unsequenced cancellation
can project as CANCELLED; the blocked requirement is applying a later cancellation
to retained conflicting booking history, not representing the cancellation enum.
Synchronization success means the unresolved evidence was safely committed; it does
not imply that planning is allowed or that the requested real-airline flow succeeded.

## 5. Stop conditions and smallest next step

The Phase 5D representation/association stop gates apply: the proposed family has
no reviewed identity and authority mapping sufficient for the required CHANGE and
CANCELLATION flows. Its documented changed-ticket workflow also raises the explicit
reissue/replacement gate; no ticket-number or same-PNR continuity rule was invented.
No claim is made that every real airline family is inherently incompatible.

Smallest recommended next step is a **bounded real-template identity/authority
contract review**, before parser code:

1. Obtain a public vendor-authored sample set or safely sanitized/reconstructed
   examples grounded in a verified single booking/change/cancellation family.
   Real user mailbox data is not required or requested.
2. Establish exact authorized traveler, stable coupon/segment, lifetime and document
   ordering semantics from that evidence. Select an alternate family if this
   candidate cannot supply them; do not promise an unverified alternate.
3. If the evidence maps directly to existing EventAuthority semantics, review the
   smallest versioned extension to the existing event-construction boundary for
   that rule. Preserve the comparator, projector, identities, cancellation barrier
   and old serialized evidence/events; a registry or new abstraction is not assumed.
4. Define opt-in admission/reprocessing behavior. Newly recognizing previously
   retained UNRESOLVED evidence must not silently alter immutable extraction JSON.
   Existing repository replay rejects changed extraction/event bytes for one key.

If real document order cannot be represented by the existing lifetime/sequence
contract, report that incompatibility for review. Do not convert receipt time,
content hashes, ticket numbers or document arrival order into synthetic sequences.
No new schema, authority type or parser-version behavior is implemented here.

## 6. Booking lifetimes, cancellation and reinstatement

The existing exact carrier/PNR/configured traveler/stable segment references remain
mandatory for reconciliation. Different explicit lifetimes remain incompatible;
an unknown lifetime is not silently assigned to an existing one. Later BOOKING or
CHANGE cannot reinstate a cancellation without the existing explicit cancellation
target linkage. Refunds, mileage restoration and ticket reissue are not assertions
of travel reinstatement. MAIL REMOVAL != FLIGHT CANCELLATION remains unchanged.

## 7. Unsupported variants and failure behavior

All real American layouts remain unsupported by this attempt. Existing extraction
still uses the frozen Northstar rule and unsupported-template fallback: suspicious
flight/booking text without a supported rule becomes UNRESOLVED; clearly unrelated
content may be NOT_TRAVEL. No claim is made that the fallback classifies every
possible real subject or language. Sender matching alone is not authentication.

No missing/conflicting field is filled, no multi-segment model is introduced, and
no new email cancellation grammar is claimed. The requested malformed-field,
layout-variation and text/HTML fixture matrix must follow a verified family rather
than manufacture a family that conveniently fits the core.

## 8. Provenance and end-to-end scope

The existing immutable message identity, MailMessage provenance, rule_id, extraction
state/segment/issues, event evidence links and revision history remain the provenance
path. No Gmail fields leak into extraction. No new raw content or secrets are stored.
The synthetic diagnostic verifies a limitation of that path; no end-to-end real
BOOKING/CHANGE/CANCELLATION success or real planning-readiness proof is claimed.

## 9. Validation and fixtures

No new airline fixtures or template tests were added because selection failed
before implementation. Existing template-focused smoke tests and all requested
regression groups were run. Detailed results are recorded in the continuation plan.
The two transient synthetic repository probes above are separate diagnostic checks,
not additional pytest cases or evidence of real-airline support.

| Validation | Result |
|---|---|
| New real-template tests | Not run: no eligible family/parser/fixtures were implemented. |
| Existing template smoke: test_v9_mail_extraction.py::MailExtractionTests | 10 passed + 23 subtests in 0.14s. |
| Extraction/reconciliation, synchronization, booking lifetime/reinstatement, persistence, migration, recovery, mail contract and provider regressions | 145 passed + 23 subtests in 9.41s. |
| Phase 5C Gmail tests | 130 passed in 3.37s. |
| Complete repository suite | 466 passed + 245 subtests in 42.98s; no failures/skips. |

Tracked/untracked whitespace and diff checks passed. Changed-file inspection and
secret-pattern review found no PII, credentials, generated artifacts, unrelated
changes or scope leakage in the two-document change set. Source/tests/fixtures and
dependency manifests are unchanged against HEAD; existing V8 source/tests remain
unchanged against v8. HEAD and frozen v7/v8 refs are unchanged, with an empty index.

No migration, dependency, Graph, calendar, FlightAware, Routes, scheduler, worker,
HITL, MCP, V8 activation, or operational flight-authority work is included.

## 10. What this slice proves and Phase 5E readiness

This slice proves that template selection alone cannot satisfy the requested real
change/cancellation transitions under the currently frozen authority boundary. It
does **not** prove the intended Phase 5D real-airline statement. Phase 5D remains
incomplete; Phase 5E has not begun and is not ready to proceed on this basis.
There is no generic airline support, production mailbox readiness, Outlook support,
live flight authority, calendar integration, real-time replanning or completed V9.
Nothing is staged, committed or pushed. Stop for the prerequisite contract decision.
