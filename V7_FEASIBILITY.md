# V7: Planning Feasibility and Explainable Scoring

Implemented on `feature/v7-feasibility`; `v6` remains the stable historical
reference. V1-V6 documentation and tags are unchanged. No commit, merge, tag,
or push is part of this implementation.

## Authority and architecture

HOST PROPOSES. CORE JUDGES.

Host LLM -> MCP -> TravelService -> Coordinator -> deterministic agents ->
TripContext -> planning core (feasibility, Critic, Beam Search).
The host is the only LLM. No model dependency, live API, host-specific logic,
calendar write, or new transport capability was introduced.

`policy.py` contains frozen `PlanningPolicy` and the shared
`DEFAULT_PLANNING_POLICY`. `timing.py` owns modeled gate arrival;
`feasibility.py` independently evaluates the hard deadline. `models.py` contains
planning-only dataclasses and dictionary serialization. Critic has one detailed
calculation; `score(context, plan)` delegates to `evaluate(context, plan).score`.
Beam Search computes feasibility before invoking Critic. Coordinator translates
the typed search outcome into the existing dictionary boundary.

## Policy and active timing

```python
modeled_gate_arrival = leave_time + timedelta(
    minutes=airport_travel_minutes + security_minutes + gate_walk_minutes
)
gate_deadline = datetime.fromisoformat(context.flight.departure_time) - timedelta(
    minutes=policy.gate_close_buffer_minutes
)
feasible = modeled_gate_arrival <= gate_deadline
```

The active baseline remains 45 + 20 + 15 = 80 minutes. Parking and terminal
walking are retrieved but remain inactive. `delay_minutes` is not added to
`departure_time`. Boarding start is only a soft scoring target.

The sole gate-buffer default is `PlanningPolicy.gate_close_buffer_minutes = 15`
in `travel_agent/planning/policy.py`. Negative values, booleans, non-integers,
and null are rejected; zero is valid. Override internally with:

```python
policy = PlanningPolicy(gate_close_buffer_minutes=25)
planner = BeamSearchPlanner(policy=policy)
coordinator = create_simulated_coordinator(policy=policy)
# Direct evaluation also accepts policy:
result = evaluate_feasibility(context, candidate, policy)
```

This configurable policy applies to the current U.S. planning scope. It does
not assert that every airline closes every gate at this offset. Actual airline
gate-close data and boarding-close data remain outside V7.

## Feasibility representation

Full local ISO timestamps preserve seconds and any fractional precision.
Feasible results have an empty `hard_constraint_failures` list. Example failure:

```json
{
  "feasible": false,
  "gate_arrival_time": "2026-09-11T18:51:00",
  "gate_deadline": "2026-09-11T18:45:00",
  "gate_close_buffer_minutes": 15,
  "deadline_source": "configured_policy",
  "hard_constraint_failures": [
    {"code": "GATE_DEADLINE_MISSED", "minutes_late": 6.0}
  ]
}
```

Arrival exactly at the deadline is feasible. Calendar conflicts, meeting
priority, early departure, and boarding buffer never become hard constraints.

## Beam Search retention and recovery

Width remains 2; depth remains 3: initial ranking plus two refinement rounds.
Each refinement pool starts with retained parents, then appends each parent's
10-minute-earlier and 10-minute-later children in beam order. Histories retain
the actual path. Equivalent parsed leave timestamps deduplicate before
evaluation, with the first encountered candidate retained, exactly as V6.

Every distinct pool candidate gets a fresh feasibility evaluation. Feasible
candidates receive Critic evaluation and sort by descending score. Infeasible
candidates receive no score or score breakdown and sort by ascending
`minutes_late`. Both sorts are stable. Concatenate feasible then infeasible,
and retain the first `beam_width` entries. Thus recovery candidates occupy only
otherwise unused slots; their lateness is never a preference score.

An infeasible seed can recover through earlier children. With a 45-minute test
policy, departure 19:00 and 80-minute modeled travel, 17:10 -> 17:00 -> 16:50
crosses from infeasible to feasible on the second refinement.

At completion, filter the beam to feasible candidates only. `search()` retains
its list-returning compatibility API; `search_outcome()` returns typed finalists
and diagnostics. Coordinator selects only from this filtered list and uses null
when it is empty. A retained feasible parent cannot be displaced by an
infeasible candidate, proving that infeasible candidates cannot be recommended.
The injected Critic test seam now uses detailed `evaluate()` results; the
production `PlanCritic.score()` compatibility method remains available.

Diagnostics count globally distinct parsed leave times actually evaluated,
including candidates pruned from a pool. Re-evaluated parents do not inflate
this count. No shared mutable last-search state is used.

## Explainable preferences

Weights and rule boundaries are unchanged:

- Base: +100.
- Boarding buffer: -100 after boarding starts; otherwise -35 below 20 minutes;
  otherwise -10 below the preferred buffer; otherwise zero.
- Early departure: -0.08 per minute beyond two hours before departure.
- Leave during an event (`start <= leave < end`): -60 high priority, -25 normal.
- Leave before or exactly at a high-priority event start: -20 independently.

At exactly a high-priority event start, both calendar rules apply. V7 explains
both deductions and does not retune or fix this pre-existing boundary.

Each feasible candidate carries `score`, `score_breakdown`, and
`calendar_conflicts`. Breakdown components include `code`, signed `value`,
and for calendar components the zero-based `event_index`. Base, boarding, and
early-departure components are always present, even when a penalty is zero.
Unrounded values preserve the existing arithmetic. The reconciliation rule is:

```python
round(sum(c["value"] for c in score_breakdown["components"]), 2) == score
```

`score_breakdown.rounding` is `"round(sum(component values), 2)"`.
Calendar records contain `event_index`, `title`, full local `start` and `end`,
`priority`, `conflict_type`, signed `penalty`, and `score_component_index`.
Conflict types are `DEPARTURE_DURING_EVENT` and
`DEPARTURE_BEFORE_OR_AT_EVENT_START`. The index links to the existing deduction;
records are explanatory and do not cause a second deduction. Calendar remains
strictly read-only: no move, reschedule, cancel, or write capability exists.

## Public result contracts

A found result has `status: "PLAN_FOUND"`, existing `mode`, `selected_plan`,
`finalists`, `recommendation`, and `diagnostics`. Selected plan equals the first
feasible finalist. Each finalist preserves proposal fields (`label`,
`leave_time`, `summary`, `history`) and adds the core's `feasibility`, `score`,
`score_breakdown`, and `calendar_conflicts`. Diagnostics include scope and count.

An unsuccessful bounded search returns a successful planning result:

```json
{
  "status": "NO_FEASIBLE_PLAN",
  "mode": "host-assisted",
  "selected_plan": null,
  "recommendation": "No feasible plan was found within the explored candidates.",
  "finalists": [],
  "diagnostics": {
    "scope": "EXPLORED_CANDIDATES",
    "evaluated_candidate_count": 4,
    "reason_code": "GATE_DEADLINE_MISSED",
    "best_infeasible_candidate": {
      "label": "Late / earlier / earlier",
      "leave_time": "2026-09-11T19:40",
      "summary": "Leave at 19:40; beam refinement depth 2.",
      "history": ["Depth 1: shifted 10 min earlier", "Depth 2: shifted 10 min earlier"],
      "feasibility": {
        "feasible": false,
        "gate_arrival_time": "2026-09-11T21:00:00",
        "gate_deadline": "2026-09-11T18:45:00",
        "gate_close_buffer_minutes": 15,
        "deadline_source": "configured_policy",
        "hard_constraint_failures": [{"code": "GATE_DEADLINE_MISSED", "minutes_late": 135.0}]
      }
    }
  }
}
```

This example starts from one 20:00 seed. Explored times are 20:00, 19:50,
20:10, and 19:40. The best infeasible candidate is diagnostic only and has no
numeric preference score. No feasible plan found in this bounded search does
not prove that no possible feasible departure exists.

TravelService signatures and validation remain unchanged. Host scores,
feasibility, breakdowns, calendar explanations, and other unknown candidate
fields are discarded. Planning independently recomputes evaluation, including
for refined children. Proposal text/history remains host-authored metadata,
not authoritative constraint or score explanation.

MCP still has exactly `get_trip_context` and `evaluate_trip_plans`. Full input
schemas compare identical to tagged v6. The output schema adds status and
diagnostics, permits null selection, and describes finalist evaluation fields.
`NO_FEASIBLE_PLAN` is `isError: false`; structured content and JSON text agree.
Malformed requests and unexpected exceptions retain existing sanitized errors.

## Verification (2026-09-08)

Command: `.venv\Scripts\python.exe -B -m unittest discover -s tests -v`.
Full suite: **108 tests passed, zero failures, zero skips**. This includes 24
new V7 test methods, expanded actual MCP stdio coverage, and retained V6 search
assertions with real context fixtures and a detailed test Critic. Existing
agent/service no-search checks now target the active `search_outcome` method.

Coverage of the requested checks:

| Requirements | Evidence |
|---|---|
| 1-10 | Gate boundaries, departure/midnight/ignored delay, boarding soft, policy override/zero/invalid/frozen shared default tests |
| 11-17 | Feasible retention over higher preference score; unused recovery slot; two-step recovery; no infeasible scoring/finalists/selection; empty core/service outcome |
| 18-23 | Retained V6 parent, child combination, deduplication, histories, stable ties, width/depth tests; V7 default checks |
| 24-35 | Reconciliation across 206 fractional timestamps; all boarding branches; unrounded early penalty; normal/high/double calendar rules; event linkage; exact gate failure records |
| 36-41 | Read-only calendar surface and context immutability; forged score/feasibility/breakdown/explanation tests; identical clean/forged results |
| 42-52 | Both service baselines; all-infeasible service and actual MCP subprocess; two-tool discovery; unchanged full input schemas; null output schema validation; six sanitized malformed stdio calls and recovery |

An additional comparison against the tagged v6 Critic evaluated **4,320**
timestamps (every minute of the day at seconds 0, 13, and 59): all scores were
identical. Actual stdio verifies deterministic, host-assisted, and all-infeasible
results against TravelService, checks output schemas, and ignores forged data.
Demos also pass with site-packages disabled and model/provider imports blocked.

All timestamps below are local on 2026-09-11:

| Mode | Role | Leave | Score | Gate arrival | Deadline | Feasible |
|---|---|---|---:|---|---|---|
| Deterministic | Selected | 16:25 | 77.20 | 17:45 | 18:45 | true |
| Deterministic | Alternative | 16:15 | 76.40 | 17:35 | 18:45 | true |
| Host-assisted | Selected | 16:20 | 76.80 | 17:40 | 18:45 | true |
| Host-assisted | Alternative | 16:10 | 76.00 | 17:30 | 18:45 | true |

## Review limitations

The search is bounded, timing is simulated, timestamps are local without zones,
and caller context validation does not authenticate real-world facts. The
configured gate buffer is policy rather than live airline evidence. These are
intentional scope limits. The existing double-calendar penalty is preserved.
Consumers must handle nullable selection; internal injected planners now provide
`search_outcome()` and injected Critics provide `evaluate()`. No broader typed
candidate migration was performed. V8 itinerary retrieval remains deferred.

Final whitespace verification: `git diff --check` passed (exit 0). Git reports
12 modified files and 6 untracked new files, all unstaged, on
`feature/v7-feasibility`. No commit, merge, tag, or push was performed.

## Final architectural review

`BeamSearchPlanner.search_outcome()` is the single search implementation;
`search()` returns its `.finalists` directly. `PlanCritic.evaluate()` is the
single scoring calculation; `score()` returns its `.score` directly. Direct
mock-based delegation tests verify a single call with the same arguments and
unchanged returned values, including empty finalists. Two service validation
tests were corrected to mock the active `search_outcome()` entry point.
No production code changed during this review.

The injected interfaces are structural (duck typed), not declared `typing.Protocol`
classes:

- Coordinator requires `search_outcome(context, initial_candidates)` returning
  `SearchOutcome` (or an equivalent object with `finalists` and `diagnostics`).
  Finalists must be feasible, ranked, evaluated plan dictionaries.
- Beam Search requires `evaluate(context, plan)` returning `ScoreEvaluation`
  (or an equivalent object whose `to_dict()` returns `score`, `score_breakdown`,
  and `calendar_conflicts`). It calls this only for feasible candidates.

These are deliberate V7 internal interface changes. Existing `search()` and
`score()` callers remain supported, but legacy injected objects implementing
only those methods must migrate. A list alone cannot describe all explored
candidates, and a scalar alone cannot supply an exact component breakdown;
automatic fallback would require incomplete diagnostics or fabricated explanations.
A future static `Protocol` could formalize these same signatures without changing
behavior; it is not needed to correct current runtime behavior.

Review verification: 32 affected tests passed (24 V7 plus 8 service); the full
108-test suite passed with no skips. A fresh 4,320-timestamp comparison called
the imported V7 `PlanCritic.evaluate()` and the Critic loaded directly with
`git show v6:travel_agent/planning/critic.py`; all scores matched, with no
test-side scoring formula. Gate arrival, gate deadline, feasibility, scoring,
and the gate-buffer default each have one production implementation/definition.
Other existing literal 15 values describe gate walking, not gate-close policy.
