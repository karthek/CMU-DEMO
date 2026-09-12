"""CP3 proposal-only policy. No approval transition or calendar-write capability.

An allowed slot is demo input, not organizer permission or attendee availability.
Only conflicts already emitted by the production Critic can produce proposals.
Calendar mutation requires explicit user approval; it is structurally unavailable
in this demo even if a host or caller expresses approval.
"""
from dataclasses import dataclass, field

from travel_agent.itinerary.clock import parse_local


CALENDAR_MUTATIONS_REQUIRE_EXPLICIT_APPROVAL = True


@dataclass(frozen=True, slots=True)
class CalendarChangeProposal:
    meeting_id: str
    meeting_title: str
    priority: str
    current_start: str
    current_end: str
    proposed_start: str
    proposed_end: str
    departure: str
    reason: str
    conflict_type: str
    penalty: float
    conflict_source: str
    approval_required: bool = field(default=True, init=False)
    approval_status: str = field(default="AWAITING_USER_APPROVAL", init=False)
    execution_status: str = field(default="NOT_EXECUTED", init=False)


def propose_calendar_changes(context, planning_result, slots, *, as_of):
    """Use exact fixture identity and allowed slots; never mutate or replan.

This is a bounded demo rule, not a scheduler: first matching permitted slot,
same duration, after the scenario clock, before departure, no overlap with the
other supplied workday events. Missing/unsuitable slots produce no proposal.
"""
    selected = planning_result.get("selected_plan")
    if selected is None:
        return ()
    proposals = []
    handled = set()
    for conflict_index, conflict in enumerate(selected["calendar_conflicts"]):
        index = conflict["event_index"]
        event = context["calendar_events"][index]
        start, end = parse_local(event["start"]), parse_local(event["end"])
        if (conflict["title"] != event["title"] or conflict["priority"] != event["priority"]
            or parse_local(conflict["start"]) != start or parse_local(conflict["end"]) != end):
            raise ValueError("Conflict does not match the supplied calendar event")
        if index in handled:
            continue
        for slot in slots:
            if (slot["meeting_title"] != event["title"]
                or parse_local(slot["current_start"]) != start
                or parse_local(slot["current_end"]) != end):
                continue
            proposed_start = parse_local(slot["allowed_start"])
            proposed_end = proposed_start + (end - start)
            if not (parse_local(as_of) <= proposed_start < proposed_end
                    <= parse_local(slot["allowed_end"])
                    and proposed_end <= parse_local(selected["leave_time"])):
                continue
            if any(proposed_start < parse_local(other["end"])
                   and parse_local(other["start"]) < proposed_end
                   for other_index, other in enumerate(context["calendar_events"])
                   if other_index != index):
                continue
            proposals.append(CalendarChangeProposal(
                meeting_id=f"fixture:calendar_events[{index}]", meeting_title=event["title"],
                priority=event["priority"], current_start=start.isoformat(), current_end=end.isoformat(),
                proposed_start=proposed_start.isoformat(), proposed_end=proposed_end.isoformat(),
                departure=selected["leave_time"],
                reason="Preserve meeting duration in the permitted demo slot before travel departure; "
                       "attendee availability and organizer permission are not verified.",
                conflict_type=conflict["conflict_type"], penalty=conflict["penalty"],
                conflict_source=f"selected_plan.calendar_conflicts[{conflict_index}]",
            ))
            handled.add(index)
            break
    return tuple(proposals)


def render_hitl(proposals):
    lines = []
    for proposal in proposals:
        lines += ["=== CALENDAR CONFLICT ===", f"Meeting: {proposal.meeting_title}",
                  f"Meeting reference: {proposal.meeting_id} (local fixture reference, not a provider ID)",
                  f"Importance [FIXTURE]: {proposal.priority}",
                  f"Conflict: {proposal.conflict_type}; recommended departure {proposal.departure}",
                  f"Source: {proposal.conflict_source}",
                  "Planner treatment: SOFT PENALTY", f"Impact: {proposal.penalty:+.2f}",
                  "DETECTED: Production evaluation reported this calendar conflict.",
                  "EVALUATED: The conflict contributed a soft score penalty; gate feasibility is unchanged.",
                  "", "=== PROPOSED CALENDAR ACTION ===",
                  f"Current: {proposal.current_start} to {proposal.current_end}",
                  f"Proposed: {proposal.proposed_start} to {proposal.proposed_end}",
                  "PROPOSED: Move this meeting into the permitted slot [DEMO INPUT].",
                  f"Reason: {proposal.reason}",
                  "The calendar is unchanged; the proposed slot has not been applied or rescored.",
                  "", "=== HUMAN APPROVAL GATE ===", "USER APPROVAL REQUIRED",
                  f"Approval required: {'YES' if proposal.approval_required else 'NO'}",
                  f"Status: {proposal.approval_status} (AWAITING EXPLICIT USER APPROVAL)",
                  f"Execution state: {proposal.execution_status}", "CALENDAR WRITE EXECUTED: NO",
                  "EXECUTED: NO. AUTHORIZATION: Explicit user approval required.",
                  "This demo has no approval transition or calendar-write capability.",
                  "Planning and policy enforcement are deterministic. The host may explain the recommendation,",
                  "but it cannot bypass the approval gate in this demo path. Independent host connectors are outside this claim."]
    return "\n".join(lines)
