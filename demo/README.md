# CMU bounded multi-domain evidence demo

AA2983, PHL -> ATL, September 29, 2026, 18:00-20:21, America/New_York.
User-supplied origin: **301 N Walnut St, Wilmington, DE**.
Python never infers the location or retrieves a route/card product.
Fixed rehearsal clock: September 29 at noon.

## Commands

Run from the repository root using the existing environment:

```powershell
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py
.\.venv\Scripts\python.exe -B demo/host_evidence_demo.py
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py --mode offline --too-late
```

The main command supports `--no-fallback` (strict MCP) and `--mode offline`.
Default fallback is infrastructure-only and visibly labeled `OFFLINE FIXTURE
FALLBACK` (legacy mode label; the three evidence domains now use recordings).
Invalid evidence, authoritative failures, schema changes and parity mismatches
never permit fallback. The host-evidence command has no fallback.

External sanitized host handoff, using temporary files outside the repository:

```powershell
.\.venv\Scripts\python.exe -B demo/host_evidence_demo.py --evidence <flight.json> --transport-evidence <transport.json> --benefit-evidence <benefit.json>
```

Each flag is independently optional. Omitting a domain explicitly selects its
labeled recorded file. Invalid supplied evidence never selects a substitute.
Use the three recorded JSON files as exact contract examples. Live flight handoff
uses `LIVE_HOST_EVIDENCE` / `GMAIL_VIA_HOST`; external transport and benefit use
`HOST_TRAVEL_EVIDENCE` and `HOST_TRAVEL_BENEFIT_EVIDENCE`. Do not relabel a recording
as a fresh retrieval. Live labels are provenance claims, not authentication.

## Evidence matrix

| Domain/capability | Current provenance or implementation |
|---|---|
| Flight | Host retrieved Gmail evidence; default is a sanitized **recording**, external live handoff supported |
| Origin | **USER-SUPPLIED**, explicitly selected address above |
| Travel distance/time | **HOST-SUPPLIED**; default `RECORDED_HOST_TRAVEL_EVIDENCE`, external replacement supported |
| Card/lounge benefit | **HOST-SUPPLIED / NORMALIZED**; default recorded host lookup |
| Lounge window | Host normalizes applicability/opening/access rules; Python validates and intersects the window |
| Calendar / permitted alternate slot | **FIXTURE** |
| Boarding time / flight status / delay / gate | **FIXTURE** supplemental operational assumptions |
| Security / gate walk / preferred buffer | **FIXTURE**, 20 / 15 / 45 minutes |
| Candidates / fixed clock | **FIXTURE**, no LLM call |
| MCP | **LIVE LOCAL STDIO** when connected |
| Validation / Beam Search / feasibility | **LIVE IMPLEMENTATION**, executed deterministically |
| Critic | **LIVE IMPLEMENTATION**, production scoring plus injected generic demo utility |
| Calendar policy / HITL | **LIVE IMPLEMENTATION**, read-only evaluation and approval-required proposals |
| Calendar mutation | **NOT AVAILABLE**, no approval-to-execution transition |
| LangSmith / remote MCP / live calendar | **FUTURE** |

Python does not access Gmail, maps, card websites, calendars or an LLM. ChatGPT
web is not directly connected to local stdio; explicit files cross that deployment
boundary. The default command replays evidence, not live provider retrieval.

## Host retrieval basis (September 13, 2026)

- Flight: the host read the Gmail trip confirmation and normalized its displayed
  AA2983 September 29 PHL 18:00 -> ATL 20:21 itinerary in the supplied local time
  basis. Only permitted normalized fields remain. Arrival and evidence timestamp
  are metadata, not planning inputs. The recording is not relabeled live each run.
- Transport: the [property page for the exact supplied address](https://www.loopnet.com/property/301-n-walnut-st-wilmington-de-19801/10003-2604320025/)
  reports Philadelphia International at **21.7 miles / 33 minutes**. This is a
  recorded host-retrieved estimate, not measured traffic or a Sep 29 forecast.
  Python has no address-to-duration rule. A fresh host lookup can replace it.
- Benefit: [Chase's lounge network](https://www.chase.com/sapphire-cards/lounges)
  identifies PHL's Terminal D/E Connector, 05:00-22:00 daily, and access within
  three hours of eligible departure. The [PHL guide](https://www.chase.com/personal/credit-cards/education/rewards-benefits/chase-sapphire-lounge-philadelphia-airport)
  describes food, work and relaxation uses. The host normalized **15:00-18:00**;
  opening hours do not further restrict that interval. Eligibility uses the user's
  card premise and remains conditional on required credentials, eligible boarding
  pass and actual admission. No membership, boarding-pass or capacity verification
  is claimed. Rules can change; host retrieval owns refreshing the evidence.

Provider/product/facility names and source references are metadata. They never
select a scoring branch. Equivalent normalized evidence with employer, other-card
or fictional provenance produces the same deterministic result.

## Implementation boundary

`FlightEvidence`, `TransportEvidence` and `TravelBenefitEvidence` are separate
immutable typed demo contracts. Origin/distance/provenance/benefit remain sidecar
facts. No production `TripContext` fields or public MCP schemas were added.

The host validates files and writes only permitted normalized fields to an
isolated temporary evidence directory. The demo server independently validates
that startup sidecar and configures one bounded session. This is **demo-local
startup configuration**, not a new public MCP benefit tool or a claim that any
existing production server can consume the sidecar.

Actual stdio flow: `tools/list -> get_trip_context -> evaluate_trip_plans`.
The main demo also calls `plan_booked_trip` and checks full result parity. Its
booked-itinerary record/status is a fixture identity, not Gmail ingestion. Only
isolated temporary attempt history is persisted, then cleaned up.

- Existing **FlightAgent** validates the normalized evidence through a demo tool,
  using fixture boarding/gate/status/delay assumptions.
- Existing **TransportAgent** validates the injected estimate; Coordinator maps
  `estimated_travel_minutes -> TransportEstimate.travel_minutes -> TripContext.airport_travel_minutes`.
  Origin and distance remain sidecar facts, not scoring inputs.
- Existing **CalendarAgent** validates explicit Sep 29 fixture events.
- A demo-local `TravelService` subclass binds external context to its validated
  evidence session, validates candidate date/clock, then delegates to unchanged
  production context validation, Coordinator, Beam Search and feasibility.
- An injected demo Critic extends production `PlanCritic`. Lounge utility affects
  **every search ranking**, not a host-side reranking after MCP returns.

All four tools and their canonical schema digest remain unchanged. The displayed
trace is a **parity-checked local production search replay with the demo Critic**;
it is **not returned by MCP**. Width 2, depth 3 means initial selection and two
+/-10-minute refinement stages.

## Exact generic lounge utility

```text
post_security = leave + travel_minutes + security_minutes
lounge_end_limit = min(boarding_time, hard_gate_deadline) - gate_walk_minutes
usable_start = max(post_security, benefit.usable_from)
usable_end = min(lounge_end_limit, benefit.usable_until)
usable_minutes = max(0, usable_end - usable_start)
```

An eligible `AIRPORT_LOUNGE` at the departure airport earns utility only with at
least **45 usable minutes**: `WORKSPACE +12`, `FOOD +6`, `RELAXATION +2`, capped
at **+20**. Absent amenities add zero; duplicates/unknown amenities are rejected.
Ineligible evidence contributes zero. Provider/card names are never consulted.
Weights are demo policy, not money. Existing base, boarding-buffer, early-departure
and calendar rules are preserved. Add the utility component before two-decimal
rounding. Hard feasibility runs first: utility cannot rescue an infeasible plan.

Gate feasibility reports direct arrival without a lounge stay. Optional lounge
use reserves the fixture gate walk before boarding/deadline, without moving either
deadline. The 15-minute walk is not a live terminal route estimate. Travel to the
lounge, parking, congestion and admission wait are not separately modeled. Lounge
use is optional utility, not a booked visit or executed action.

| Default result | Base Critic | Lounge minutes | Utility | Total |
|---|---:|---:|---:|---:|
| Recommended 15:30 | 77.60 | 52 | +20.00 | **97.60** |
| Alternative 15:20 | 76.80 | 62 | +20.00 | **96.80** |

Recommendation: `100 + 0 - 2.40 - 20 + 20 = 97.60`. These are execution-derived
acceptance observations, not host-supplied scores. External evidence may change
results; it is not forced to this baseline. Initial proposals: 15:10, 15:50, 16:30
(the weak candidate). A separate test isolates a case where utility changes the
preferred departure while preserving feasibility.

## Calendar and refusal

Fixture events: Project Review 13:00-14:00; Leadership Meeting 16:00-16:30.
Leaving before the important meeting incurs a **soft -20** penalty. The proposed
**14:15-14:45** slot preserves duration; attendee availability/organizer permission
are unverified. It is neither applied nor rescored. The unchanged policy stops at:

```text
AWAITING_USER_APPROVAL
NOT_EXECUTED
CALENDAR WRITE EXECUTED: NO
```

The too-late case supplies Sep 29 18:00 and its equivalent seconds spelling. Even
earlier explored refinements miss the 17:45 gate deadline with recorded timing.
Result: `NO_FEASIBLE_PLAN`, no recommendation, proposal or calendar write. This
explicit offline case does not attempt MCP. Infeasible retained nodes are search
exploration only; recommendation requires feasible finalists.

## Validation and privacy

Transport requires the exact supplied origin, matching airport/date, DRIVE mode,
positive integer minutes (1-1440), and positive finite miles (<=2000). Benefit
windows must be ordered, on the trip date and end no later than departure.
Wrong-airport/date/malformed evidence is **rejected**, not ignored. All domain
fields are required except the flight contract's optional arrival/timestamp.
Duplicate JSON keys, missing/unknown fields, invalid types/source labels fail
closed. External `null` never means use a recorded default.

Never provide/persist Gmail IDs, raw email, email addresses, passenger names,
confirmation codes, payment data, loyalty/membership numbers, card/account numbers
or billing information. These fields are not accepted. Only the explicitly
selected street origin is permitted. Provenance descriptions/references must be
non-personal. Error output is sanitized; raw payloads are not logged.

Focused checks:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_cmu_demo.py tests/test_cmu_rehearsal.py tests/test_cmu_mcp_demo.py tests/test_host_evidence_demo.py tests/test_demo_context_evidence.py -q
```

Tests cover actual stdio parity, external files, strict invalid evidence, utility
boundaries/arithmetic, transport-driven infeasibility, brand neutrality,
determinism and the absent calendar write surface. Production regressions remain
separate. No production source/API/schema or production branch is changed.
