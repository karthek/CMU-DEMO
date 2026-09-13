"""CP6A recorded structured evidence over existing MCP; no direct fallback."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryFile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client import Client
from mcp.client.stdio import stdio_client
from demo.host_evidence import (EvidenceSource, EvidenceValidationError, parse_evidence,
                                validate_scenario_evidence, evidence_to_context)
from demo.mcp_host_demo import server_parameters, checked_call, EXPECTED_TOOLS, SCHEMA_DIGEST
from demo.rehearsal_policy import CompatibilityFailure, validate_baseline
from demo.hitl import propose_calendar_changes, render_hitl

ROOT = Path(__file__).resolve().parents[1]


def load_payload(path=None):
    try:
        def unique_fields(pairs):
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("Duplicate JSON fields")
            return result
        return json.loads(Path(path or Path(__file__).with_name("recorded_host_evidence.json")).read_text(
            encoding="utf-8"), object_pairs_hook=unique_fields)
    except (ValueError, OSError) as exc:
        raise EvidenceValidationError("Cannot load structured evidence JSON") from exc


async def rehearse_evidence(payload=None, *, parameters=None):
    scenario = json.loads((ROOT / "demo/scenario.json").read_text(encoding="utf-8"))
    evidence = parse_evidence(load_payload() if payload is None else payload)
    # Source vocabulary supports future labeling; CP6A never claims live access.
    if evidence.source_type != EvidenceSource.RECORDED:
        raise EvidenceValidationError("CP6A rehearsal accepts RECORDED HOST EVIDENCE only; live use is not enabled")
    validate_scenario_evidence(evidence, scenario)  # Before any MCP process starts.
    with TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        async with asyncio.timeout(45), Client(stdio_client(parameters or server_parameters(), errlog=diagnostics)) as client:
            discovered = (await client.list_tools()).tools
            tools = {tool.name: tool for tool in discovered}
            digest = hashlib.sha256(json.dumps({t.name: [t.input_schema, t.output_schema]
                for t in discovered}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if set(tools) != EXPECTED_TOOLS or len(discovered) != len(EXPECTED_TOOLS) or digest != SCHEMA_DIGEST:
                raise CompatibilityFailure("Existing MCP schema/tool compatibility changed")
            supplemental = await checked_call(client, tools, "get_trip_context", {
                "flight_number": evidence.flight_number, "date": evidence.departure_date})
            context, provenance = evidence_to_context(evidence, supplemental)
            result = await checked_call(client, tools, "evaluate_trip_plans", {
                "context": context, "candidates": scenario["candidates"]})
            validate_baseline(result)
            proposals = propose_calendar_changes(context, result, scenario["calendar_proposal_slots"],
                                                 as_of=scenario["as_of"])
            return {"evidence": evidence, "context": context, "provenance": provenance,
                    "planning_result": result, "proposals": proposals,
                    "tools": tuple(sorted(tools))}


def render_evidence(report):
    evidence = report["evidence"]
    selected, alternative = report["planning_result"]["finalists"]
    return "\n".join(["=== HOST EVIDENCE ===", f"Source: [{evidence.source_label}]",
        f"Provider: {evidence.provider.value}", f"Flight: {evidence.flight_number}",
        f"Route: {evidence.departure_airport} -> {evidence.arrival_airport}",
        f"Scheduled departure: {evidence.scheduled_departure} ({evidence.time_basis})",
        f"Scheduled arrival: {evidence.scheduled_arrival or 'not supplied'} (retained evidence; not a planner input)",
        f"Evidence timestamp: {evidence.evidence_timestamp or 'not supplied'} ({evidence.time_basis})",
        "Validation: PASSED (structure and demo compatibility; not verified booking authenticity)",
        "Gmail was NOT accessed. No LLM was called.",
        "=== FIELD PROVENANCE [DEMO SIDECAR] ===",
        *[f"{field}: [{source}]" for field, source in report["provenance"].items()],
        "Candidates: [FIXTURE] rehearsed host-style proposals; not authoritative evaluations.",
        "=== DECISION AUTHORITY ===", "HOST SUPPLIES EVIDENCE. PYTHON MAKES THE DECISION.",
        "MCP flow: tools/list -> get_trip_context -> evaluate_trip_plans",
        "Host bridge: validates and maps structured booking fields; retains provenance outside MCP.",
        "MCP Travel Agent: accepts structured context and revalidates it in TravelService.",
        "Deterministic Python: evaluates feasibility and alternatives. No booking ingestion/persistence claimed.",
        "=== RESULT [MCP] ===", f"Recommended: {selected['leave_time']} / {selected['score']:.2f}",
        f"Alternative: {alternative['leave_time']} / {alternative['score']:.2f}",
        "=== DEMO GOVERNANCE [POST-MCP] ===", render_hitl(report["proposals"]),
        "Source labels describe supplied provenance, not authenticated email or provider authority.",
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Recorded structured booking JSON")
    args = parser.parse_args(argv)
    try:
        print(render_evidence(asyncio.run(rehearse_evidence(load_payload(args.evidence)))))
    except Exception as exc:
        # Do not echo email-derived content, paths, or exception groups.
        def invalid_evidence(error):
            return isinstance(error, EvidenceValidationError) or (
                isinstance(error, BaseExceptionGroup) and any(invalid_evidence(child) for child in error.exceptions))
        category = "INVALID HOST EVIDENCE" if invalid_evidence(exc) else "HOST EVIDENCE REHEARSAL FAILED"
        print(f"{category}: validation, compatibility, or MCP failure; no fallback or answer fabricated.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
