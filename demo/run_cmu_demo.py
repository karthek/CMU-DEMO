"""Single presentation launcher. Fallback is authorized only by failure type."""
import argparse
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryFile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.cmu_demo import run_demo
from demo.hitl import render_hitl
from demo.search_trace import render_trace
from demo.rehearsal_policy import (InfrastructureFailure, AuthoritativeFailure,
                                   RehearsalFailure, validate_baseline)


def run_mcp():
    # Lazy import keeps deliberate offline mode independent of optional MCP SDK.
    try:
        from demo.mcp_host_demo import rehearse
    except ModuleNotFoundError as exc:
        if exc.name.split(".")[0] in {"mcp", "jsonschema"}:
            raise InfrastructureFailure("Optional MCP runtime dependency unavailable") from exc
        raise
    # Capture child stderr so infrastructure failure does not flood the classroom
    # terminal. The typed exception remains visible; no direct fallback in CP4.
    with TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        return asyncio.run(rehearse(errlog=diagnostics))


def rehearse_demo(*, mode="mcp", fallback=True, mcp_runner=None, direct_runner=None):
    if mode not in {"mcp", "offline"}:
        raise ValueError("Unknown demo mode")
    mcp_runner = mcp_runner or run_mcp
    direct_runner = direct_runner or run_demo
    remote = None
    reason = None
    if mode == "mcp":
        try:
            remote = mcp_runner()
        except InfrastructureFailure as exc:
            if not fallback:
                raise
            # Exception text may originate from external/runtime data. The
            # presentation uses a fixed sanitized reason, never arbitrary text.
            reason = "Local MCP infrastructure unavailable (startup, transport, tool availability, or response structure)."
        if remote is not None:
            validate_baseline(remote["planning_result"])
    # On MCP success this is an observational CP2 replay, NOT a fallback. Full
    # parity is mandatory before its trace can be displayed beside MCP evidence.
    try:
        direct = direct_runner()
    except Exception as exc:
        raise AuthoritativeFailure("Local deterministic path failed; demo stopped without an answer") from exc
    result = direct["booked_result"]["run"]["planning_result"]
    validate_baseline(result)
    if remote is not None:
        validate_baseline(remote["planning_result"])
        if (remote["booked_result"] != direct["booked_result"]
            or remote["proposals"] != direct["calendar_proposals"]):
            raise AuthoritativeFailure("Direct/MCP evidence or governance parity failed; no fallback")
        # Present MCP evidence; use only the parity-checked direct trace.
        direct = dict(direct, booked_result=remote["booked_result"], calendar_proposals=remote["proposals"])
    return {"mode": "MCP / LOCAL STDIO" if remote is not None else "OFFLINE FIXTURE FALLBACK",
            "reason": reason, "intentional_offline": mode == "offline", "demo": direct}


def too_late_demo():
    """Rehearse the existing CP2 18:00 duplicate test through the real service."""
    report = run_demo(candidates=[
        {"label": "Late", "leave_time": "2026-09-16T18:00", "summary": "Too late"},
        {"label": "Duplicate", "leave_time": "2026-09-16T18:00:00", "summary": "Same instant"}])
    result = report["booked_result"]["run"]["planning_result"]
    if result["status"] != "NO_FEASIBLE_PLAN" or result["selected_plan"] is not None:
        raise AuthoritativeFailure("Too-late rehearsal did not return the expected NO_FEASIBLE_PLAN")
    return "\n".join(["=== OFFLINE TOO-LATE REHEARSAL ===", "Explicit CP2 fixture case; MCP not attempted.",
        render_trace(report["trace"], report["beam_width"], report["depth"], result["finalists"]),
        "AUTHORITATIVE RESULT: NO_FEASIBLE_PLAN", result["recommendation"],
        "No recommendation fabricated. No fallback triggered. CALENDAR WRITE EXECUTED: NO."])


def render_rehearsal(rehearsal):
    report = rehearsal["demo"]
    context = report["booked_result"]["run"]["context"]
    result = report["booked_result"]["run"]["planning_result"]
    selected, alternative = result["finalists"]
    proposal = report["calendar_proposals"][0]
    lines = ["=== CMU TRAVEL AGENT DEMO ===", "MODE: " + rehearsal["mode"],
             f"Fixed clock [FIXTURE]: {report['scenario']['as_of']} America/New_York",
             "=== MCP STATUS ==="]
    if rehearsal["mode"] == "MCP / LOCAL STDIO":
        lines += ["Status: CONNECTED | Transport: LOCAL STDIO",
                  "Tool flow: tools/list -> get_trip_context -> evaluate_trip_plans -> plan_booked_trip",
                  "Authoritative evaluation source: MCP TRAVEL AGENT"]
    else:
        lines += ["MCP host: NOT ATTEMPTED (explicit offline mode)" if rehearsal["intentional_offline"] else "MCP host: UNAVAILABLE",
                  "Reason: " + (rehearsal["reason"] or "Presenter selected offline mode."),
                  "Fallback: ENABLED | Source: LOCAL DETERMINISTIC FIXTURE PATH",
                  "Fallback changes the integration path, not the planning logic."]
    lines += ["=== 1. BOOKED TRIP [FIXTURE] ==="]
    for booking in report["scenario"]["itineraries"]["records"]:
        for segment in booking["segments"]:
            lines.append(f"{booking['itinerary_id']}: {segment['flight_number']} {segment['origin']} -> "
                         f"{segment['destination']} | {segment['scheduled_departure']} | {segment['booking_status']}")
    lines += ["=== 2. CONTEXT [FIXTURE] ===",
              f"Travel {context['airport_travel_minutes']} + security {context['security_minutes']} + "
              f"gate walk {context['gate_walk_minutes']} minutes; parking is not added."]
    lines += [f"{e['title']}: {e['start']} to {e['end']} ({e['priority']})" for e in context["calendar_events"]]
    lines += ["=== 3. HOST-STYLE CANDIDATES ==="]
    lines += [f"{p['label']}: {p['leave_time']} (proposal only)" for p in report["scenario"]["candidates"]]
    lines += ["=== 4. DETERMINISTIC EVALUATION ===",
              f"Recommended {selected['leave_time']} / {selected['score']:.2f}; "
              f"alternative {alternative['leave_time']} / {alternative['score']:.2f}",
              f"Gate feasible: {selected['feasibility']['feasible']}; modeled arrival "
              f"{selected['feasibility']['gate_arrival_time']}; deadline {selected['feasibility']['gate_deadline']}",
              "Best within explored candidates; feasibility is relative to configured assumptions.",
              "=== 5. BEAM SEARCH ===",
              "Trace source: parity-checked LOCAL production execution replay; not returned by MCP."
              if rehearsal["mode"] == "MCP / LOCAL STDIO" else "Trace source: actual local production execution.",
              render_trace(report["trace"], report["beam_width"], report["depth"], result["finalists"]),
              "=== 6. DEMO GOVERNANCE (post-evaluation, not an MCP tool) ===",
              render_hitl(report["calendar_proposals"]), "=== FINAL RECOMMENDATION ===",
              f"Leave: {selected['leave_time'][-5:]} | Score: {selected['score']:.2f}",
              f"Alternative: {alternative['leave_time'][-5:]} / {alternative['score']:.2f}",
              f"Calendar conflict: {proposal.meeting_title} | SOFT PENALTY ({proposal.penalty:+.2f})",
              f"Proposed adjustment: {proposal.proposed_start[11:16]}-{proposal.proposed_end[11:16]}",
              f"Approval: REQUIRED / {proposal.approval_status} | Execution: {proposal.execution_status}",
              "CALENDAR WRITE EXECUTED: NO",
              "Proposed slot is demo input; attendee availability and organizer permission are not verified.",
              "=== ARCHITECTURE ===", "HOST: proposes / invokes / communicates",
              "MCP TRAVEL AGENT: validates / evaluates / constrains (bypassed only in labeled offline mode)",
              "DETERMINISTIC PYTHON: owns scoring, feasibility, Beam Search, and policy enforcement",
              "CALENDAR GOVERNANCE: explicit approval required; no mutation capability or execution path."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mcp", "offline"), default="mcp")
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--too-late", action="store_true", help="Offline CP2 NO_FEASIBLE_PLAN rehearsal")
    args = parser.parse_args(argv)
    if args.too_late and args.mode != "offline":
        parser.error("--too-late requires --mode offline")
    try:
        print(too_late_demo() if args.too_late else render_rehearsal(
            rehearse_demo(mode=args.mode, fallback=not args.no_fallback)))
    except Exception as exc:
        # Typed failures contain controlled messages; never print raw exceptions.
        category = "INFRASTRUCTURE FAILURE" if isinstance(exc, InfrastructureFailure) else "DEMO STOPPED"
        message = str(exc) if isinstance(exc, RehearsalFailure) else "Unexpected runtime failure; no answer fabricated."
        print(f"{category}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
