# V9 Phase 5F-A: provider-neutral calendar read contract

Review/implementation date: 2026-09-12. Repository: model_agnostic_travel_agent_v1.
Branch: `feature/v9-live-replanning`. Verified clean starting HEAD:
`28ea3af8e47630a366f7029dccb1d5de1eb03d32` (frozen Phase 5E-B).
This phase remains uncommitted. No push or Phase 5F-B implementation.

## 1. Purpose and scope

Answer: **What calendar commitments constrain or conflict with the supplied
travel/departure window?** This is a deterministic, scoped read contract, not a
calendar application, transportation model, action-authority service or scheduler.

```text
future Google / Graph / other adapter, or FixtureCalendarProvider
    -> live.calendar.CalendarProvider.read_events(CalendarQuery)
    -> ProviderResult[CalendarSnapshot[NormalizedCalendarEvent]]
    -> CalendarReadAgent
    -> CalendarConflictEvaluator
    -> structured constraints for the supplied travel window
```

No HTTP, OAuth, API access, provider SDK, model SDK, persistence, recurrence engine,
write workflow, MCP tool, host intake or live-replanning wiring is implemented.
Existing mail providers, extraction, booking and V7/V8 behavior are unchanged.

## 2. Official-source grounding and provider differences

Reviewed 2026-09-12. These references ground future adapter requirements; they
do not establish live conformance for this offline phase:

- [Google event resource](https://developers.google.com/workspace/calendar/api/v3/reference/events):
  event IDs differ from iCalUID; recurring instances share iCalUID. The original
  recurrence slot identifies a moved instance independently of its new schedule.
  Cancellation records can omit normal event details. All-day values use civil
  dates; event ends are exclusive. Attendance response and time transparency are
  distinct evidence.
- [Google bounded event listing](https://developers.google.com/workspace/calendar/api/v3/reference/events/list):
  list filtering and singleEvents expansion are relevant to a future adapter.
  It must complete paging and cover overlapping instances, not only starts.
- [Microsoft Graph event resource](https://learn.microsoft.com/en-us/graph/api/resources/event?view=graph-rest-1.0):
  distinguishes single instances, masters, occurrences and exceptions; originalStart
  and seriesMasterId supply recurrence context. Graph iCalUId differs per occurrence.
  Default IDs can change with container moves. Its all-day timestamps use midnight
  in one timezone. showAs, isCancelled and isOrganizer are separate facts.
- [Graph immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id):
  a future Graph calendar adapter must evaluate the documented immutable-ID profile
  and its mailbox-lifetime limits, rather than promising globally immutable IDs.
- [Graph responseStatus](https://learn.microsoft.com/en-us/graph/api/resources/responsestatus?view=graph-rest-1.0):
  organizer, accepted, declined, tentative and unanswered states are distinct;
  none must not automatically become accepted.
- [Graph calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0):
  bounded occurrence/exception retrieval is the relevant future read mechanism.

**Project decisions below** define neutral identity, policy, freshness and failure
behavior. They are not claims that both APIs expose identical objects or guarantees.
Exact adapter mapping, default-field interpretation, timezone mapping, permissions,
account binding, cancellation coverage and paging must be reviewed in 5F-B.

## 3. Normalized event model

Frozen dataclasses and typed enums live in `travel_agent/live/calendar.py`.

| Field/type | Purpose |
|---|---|
| CalendarScope(provider, account_id, calendar_id) | Explicit evidence/query isolation; opaque native identities, no display-name matching. |
| CalendarEventIdentity | Native event resource ID, typed kind and optional series/occurrence keys; audit and later exact refetch. |
| period: TimeInterval, AllDaySpan, or None | Timed commitment, civil all-day coverage, or unresolved timing; no guessed duration. |
| EventStatus | CONFIRMED, TENTATIVE, CANCELLED, REMOVED, UNKNOWN. |
| CalendarAvailability | BUSY, FREE, TENTATIVE, UNKNOWN; distinct from RSVP and event status. |
| TravelerRole | ORGANIZER, ATTENDEE, NON_PARTICIPANT, UNKNOWN. |
| Participation | ACCEPTED, TENTATIVE, DECLINED, NEEDS_ACTION, NOT_APPLICABLE, UNKNOWN. |
| Provenance | Existing source/aware observed_at/RetrievedBy convention. |
| source_version, source_modified_at | Optional opaque concurrency/audit facts, never identity or chronological authority by token sorting. |
| source_timezone | Optional validated IANA metadata; required to agree with an all-day span's zone. |
| organizer_id | Optional opaque organizer reference for explanation and later refetch/action assessment. |
| title, location | Optional bounded explanation fields; no title matching, geocoding, commute inference or meeting-description storage. |

Fields are single-line and bounded to 1,024 characters where textual. No attendee
roster, conferencing credentials, meeting body, join URL, raw API object or secret
is retained. `all_day` is derived from the period type, preventing a contradictory
boolean/time representation. Missing private titles do not invalidate valid busy
evidence. Missing required identity is invalid; unknown time remains uncertainty.

Role/participation must concern the configured traveler bound to the account,
not an arbitrary attendee or the owner of a shared calendar. Calendar ownership
alone does not establish traveler attendance. An adapter unable to prove the
relationship must emit UNKNOWN or fail, not default to accepted/nonparticipant.

## 4. Identity and recurrence

Identity is scoped by provider/account/calendar. SINGLE and SERIES_MASTER keys use
their typed kind and native event ID. An OCCURRENCE or EXCEPTION requires both a
series_id and occurrence_id; its key is scope + `occurrence` + series_id +
occurrence_id. Event resource ID remains available separately for provider refetch.
Thus a moved/modified exception can keep the original occurrence identity even
when its current scheduled time or resource representation changes.

occurrence_id is a stable, source-grounded slot identifier supplied by the adapter,
not a guessed identifier from title/current start. A verified original recurrence
date/instant can be encoded as a typed opaque slot token by an adapter. Exact
encoding/continuity needs provider tests. If continuity cannot be established,
do not synthesize or merge it; reject unsupported recurrence evidence.

Version, title, current start/end, RSVP and cancellation are not membership keys.
No cross-account, cross-calendar or cross-provider deduplication by iCalUID, title
or time is implemented. Calendar moves may change scope; a globally continuous
cross-calendar identity is not promised. Series replacement/splitting is not
inferred to be the same series lifetime.

The model can identify a master for audit, but a successful CalendarSnapshot cannot
contain one: adapters must expand recurring commitments over the query window and
apply exceptions/cancellations before returning a complete result. No RRULE is
interpreted by the core. One current normalized value per identity is required;
duplicates/conflicting versions fail rather than using arrival order. A cancelled
occurrence does not cancel siblings and may omit its period.

## 5. Provider query and completeness contract

The **new active read protocol** is fully qualified as
`travel_agent.live.calendar.CalendarProvider`:

```python
def read_events(self, query: CalendarQuery) -> ProviderResult[CalendarSnapshot]: ...
```

CalendarQuery contains one explicit scope, an aware positive TimeInterval and
max_events. Project bounds: at most 31 elapsed UTC days and 1..1,000 events, default
1,000. Smaller caller bounds are allowed. These are core resource policy, not
provider limits. A result must cover events overlapping the interval, including
ones that started earlier, and must not clip their actual periods to the query.
Unknown-time evidence that cannot safely be excluded must remain represented or
cause failure. All provider pages/expansion must finish before success.

CalendarSnapshot contains the exact query, immutable sorted event tuple and aware
retrieved_at. It denotes a complete read, never a delta, page, truncated list or
cache fallback. Construction validates scope, count, unique identity, observation
time and absence of unexpanded masters. Completeness is also a provider obligation;
the core cannot independently detect an adapter falsely claiming a complete empty
response. Future adapter tests must prove this obligation.

Failures use existing ProviderResult/ProviderError, including retryability and
Retry-After. Failure never becomes an empty successful calendar. No transport
infrastructure is duplicated here. Bad caller query/coverage raises ValueError
before I/O; malformed/wrong-query provider results map to INVALID_RESPONSE, raw
exceptions are suppressed, and unexpected TimeoutError maps to TIMEOUT.

Coverage is **only the named scope/window**. Future orchestration must enumerate
all configured required scopes and require reliable results from each. A successful
empty read from one calendar is not a claim that every connected calendar is clear.
Aggregation, account authentication, provider pagination and durable delta sync
are not implemented by this phase.

## 6. Time, DST and interval boundaries

TimeInterval validates through existing `live.time.utc` before comparing endpoints.
Every instant is aware and comparisons/arithmetic use UTC. Source IANA metadata
is retained separately when known; offset-only timed evidence need not invent an
IANA zone. Provider adapters must validate any source offset/zone agreement before
discarding the raw representation, using the existing source time utilities.
No America/New_York or machine-local default is added.

Ambiguous source wall times need explicit offset/fold evidence; nonexistent times
are rejected by existing helpers. UTC arithmetic correctly orders two instants
within a repeated DST hour even when their local clock readings run backward.

All intervals use **[start, end)**. Overlap requires
`max(event.start, travel.start) < min(event.end, travel.end)`.
Meeting end == travel start and meeting start == travel end are non-conflicts.
Starting together with positive overlap is a conflict when blocking policy applies.
Zero/negative windows are invalid, not empty success.

## 7. All-day policy

AllDaySpan stores start_date and exclusive end_date as dates with an explicit IANA
zone or unknown zone. When the zone is known, local civil boundaries determine the
coverage interval; it is not a midnight meeting or a fixed 24-hour duration.
US DST transition days can cover 23 or 25 hours. Ambiguous/nonexistent boundaries
remain unresolved instead of choosing an offset. Missing zone is UNKNOWN_TIME
unless explicit nonblocking facts already settle the item.

Default policy respects availability: a known busy, applicable all-day commitment
constrains the overlapping window; explicit free does not. Unknown availability
is UNKNOWN, not busy/free guessed from title or event type. An explicit policy can
ignore otherwise known busy all-day spans; it does not turn unresolved participation,
availability or timing into an all-clear.

## 8. Conflict/participation policy

CalendarPolicy defaults: tentative_blocks=True, all_day_blocks_when_busy=True,
max_age=15 minutes. These are explicit project choices, configurable at evaluation.
No transportation duration, score penalty or automatic planning activation is added.

| Evidence | Effect |
|---|---|
| Explicit CANCELLED, including occurrence tombstone | IGNORED; no sibling mutation. |
| REMOVED/visibility-only marker not resolved to cancellation | UNKNOWN; never infer an event cancellation or a reliable clear window from it. |
| Known period outside travel interval | IGNORED. |
| Explicit FREE | IGNORED, even if other attributes are unnecessary/unknown. |
| Explicit NON_PARTICIPANT | IGNORED; cannot be inferred from missing attendees. |
| ATTENDEE + DECLINED | IGNORED. |
| Missing/unresolvable period or status | UNKNOWN where not already settled by explicit nonblocking facts. |
| Unknown role, unanswered/unknown attendee RSVP, contradictory organizer RSVP | UNKNOWN. |
| ORGANIZER | Own commitment; RSVP can be not applicable/unknown, but declined/needs-action is contradictory. Role is not permission to write. |
| Applicable confirmed/busy accepted or organizer-owned event | BLOCKING. |
| Applicable tentative status, RSVP or availability | BLOCKING by default; explicit tentative policy can ignore it. |
| Unknown availability | UNKNOWN, not a silent default. |

The result is CONFLICT if any known blocking item exists; otherwise UNKNOWN if any
relevant uncertainty exists; otherwise NO_CONFLICT. A mix of known conflict and
unknown evidence remains CONFLICT with reliable=False. Consumers must inspect both
state and reliability. Only reliable NO_CONFLICT establishes a clear queried window.

Snapshot retrieval and every event observation must be no later than explicit as_of
and no older than policy.max_age (boundary inclusive). Stale/future evidence yields
UNKNOWN with STALE_OR_FUTURE_EVIDENCE. A fresh cache access cannot make an old event
observation fresh. Source modification time is optional and cannot follow observation.

## 9. Structured results and future replanning

CalendarConflictResult records query/coverage, supplied travel_window, policy,
state, ordered CalendarAssessment evidence, issues and derived reliability.
Each assessment retains the normalized source event, effect, typed reason and
exact overlap when known. This provides provider/account/occurrence identity,
source version, provenance and structured explanations without requiring prose.

Derived `calendar-conflict/v1:sha256:` tokens describe blocking identity, effective
event interval, overlap and reason. Title/source-version-only changes do not change
these tokens; a changed conflicting schedule does. These tokens are **not** event
identity, provider versions, authorization or persisted evidence fingerprints.

Later orchestration can assemble PlanSnapshot.active_window_conflicts from reliable
current-window results and compare against the latest ACTIVE plan, not a prior poll.
Unknown results/provider failures must also feed missing-required-data handling;
mapping only the token set would incorrectly erase uncertainty. No integration
into ReplanningEvaluator is performed now. Tokens do not authorize actions.

The planning/orchestration layer supplies both the candidate/recommended departure
and the appropriate completion boundary. It chooses which activity interval matters
for the actual recommendation. CalendarReadAgent only checks coverage and delegates
evaluation; it never invents a trip duration, gate arrival, flight end or buffer.

## 10. Frozen CalendarAgent / TripContext compatibility

`travel_agent.agents.calendar_agent.CalendarAgent` and TripContext intentionally
remain their frozen V8 local-string models. Existing tests require that CalendarAgent
reject aware input and retain exactly its existing four public read/reason methods.
Silently inserting aware V9 events into this boundary would violate that contract.

The narrow opt-in V9 counterpart is
`travel_agent.live.calendar_agent.CalendarReadAgent(provider)`, with
`evaluate_window(query, travel_window, *, as_of, policy)`. It preserves CalendarAgent's
read-and-reason responsibility while using normalized aware evidence and typed
results. Existing entry points and V7 soft calendar scoring remain untouched.
There is no automatic legacy conversion or activation wiring. The compatibility
test exercises both boundaries without modifying either legacy source or tests.

Likewise, the earlier `live.providers.CalendarProvider` list/get scaffold,
CalendarEvent and CalendarAuthority remain unchanged for frozen imports/tests.
They are **not** the new 5F-A completeness contract and are not accepted implicitly
by CalendarReadAgent. New adapters must import `live.calendar.CalendarProvider`.
No old can_cancel/can_reschedule flag is promoted into new action authority.

## 11. Reads versus future HITL

**Calendar reads may be autonomous. Calendar mutations may NEVER be autonomous.**
Agent prepares. Human commits.

CalendarProvider exposes read_events only. CalendarReadAgent and the fixture have
no create/update/reschedule/delete/decline/accept methods, even as placeholders.
No permission/allowed-action decisions, credentials or approval workflow are added.
Read credentials must remain separable from future write credentials.

Future CalendarActionService must refetch the exact scoped event/occurrence, verify
current version, organizer/attendee role, provider permissions and explicit human
approval before mutation. The optional version/organizer facts here aid that
assessment but cannot establish current permission or authorization. Missing version
or unknown role requires refetch/validation, not an autonomous fallback. Candidate
action types belong to that later separately reviewed service.

## 12. Fixture provider and validation

FixtureCalendarProvider is an immutable one-scope in-memory provider with supplied
events and observation time. It performs deterministic overlap selection, retains
unknown-time/removal evidence, rejects duplicate identities before filtering, and
returns either a complete bounded CalendarSnapshot or existing neutral error.
It has no clocks that silently advance freshness, API calls, recurrence generation,
mutation methods, durable state or large fixture framework.

`tests/test_v9_calendar.py` adds **48 focused tests** covering the requested aware
times, New York/Los Angeles overlap, exact interval boundaries, policy/participation,
all-day, recurrence/exception/cancellation identity, duplicates, scope and query
bounds, multiple/empty events, deterministic evidence, semantic conflict tokens,
fixture behavior, errors/freshness, legacy compatibility, read-only surfaces and
DST fold/gap/civil-day behavior. All identities are invented and network is prohibited.
This is within the requested test budget; generic HTTP behavior is not re-proved.

Final validation (2026-09-12):

- New calendar focused tests: **48 passed**.
- New calendar plus existing V9 provider contracts: **52 passed in 0.45s**
  (48 new + 4 existing).
- Existing CalendarAgent/planner/V7 feasibility/service/V8 MCP/time/replanning:
  **68 passed + 61 subtests in 19.77s**.
- Complete suite: **749 passed + 245 subtests in 37.40s**, no failures or skips.

Final whitespace/diff checks passed, including untracked files. The exact five-file
scope is this contract, the plan, two new calendar modules and one new test module;
the index is empty. Secret/PII/artifact and import reviews passed. No dependency or
migration changed. All existing source/tests and all 62 Python source/test files
present in the v8 tag remain unchanged. No HTTP/provider API, persistence, mutation,
transportation calculation or model SDK import appears in the new calendar modules.

Conclusion: **A. PHASE 5F-A CALENDAR READ CONTRACT APPROVED FOR COMMIT**.
This is a review conclusion; the user has not authorized committing this phase.

## 13. Limitations and Phase 5F-B

This phase establishes the normalized deterministic contract and focused evaluator;
it does not establish Google/Graph production coverage or a live complete calendar.
No database migration/dependency is needed because these are in-memory read/results
using existing time/provenance/error infrastructure. No persistence was introduced.

Unexpanded recurrence, unclear identity continuity, unknown source zones, unanswered
attendance, removed markers, stale data and partial failures deliberately limit
availability. Cross-calendar aggregation, shared-calendar traveler binding, provider
ID profiles, provider version validation, expanded recurrence completeness and API
normalization need adapter-specific review/testing in the next slice. Unknowns must
not be resolved through LLM inference, title matching or current-time identity guesses.

Phase 5F-B may separately design/implement bounded read-only Google and Microsoft
calendar adapters behind this exact read contract after permission/account/timezone/
recurrence/completeness review. It must reuse existing transport/error/bounds concepts
and preserve the legacy boundary. No 5F-B, calendar write/HITL implementation,
FlightAware, Routes, scheduler or live-replanning work begins in this run.
