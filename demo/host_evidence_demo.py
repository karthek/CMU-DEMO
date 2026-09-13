"""Recorded evidence or explicit live host file handoff over MCP; no fallback."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryFile, TemporaryDirectory
from dataclasses import asdict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client import Client
from mcp.client.stdio import stdio_client
from demo.host_evidence import (EvidenceSource, EvidenceValidationError, parse_evidence,
                                validate_scenario_evidence, evidence_to_context)
from demo.mcp_host_demo import server_parameters, checked_call, EXPECTED_TOOLS, SCHEMA_DIGEST
from demo.rehearsal_policy import CompatibilityFailure, validate_baseline
from demo.hitl import propose_calendar_changes, render_hitl
from demo.context_evidence import TransportEvidence, TravelBenefitEvidence, parse_domain, read_json
from demo.composition import DemoEvidence, render_domains
from demo.rehearsal_policy import validate_result

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


async def rehearse_evidence(payload=None, *, parameters=None, allow_live=False,
                            transport_payload=None, benefit_payload=None):
    scenario = json.loads((ROOT / "demo/scenario.json").read_text(encoding="utf-8"))
    evidence = parse_evidence(load_payload() if payload is None else payload)
    # Live provenance requires an explicit handoff; the recorded default stays offline.
    if evidence.source_type == EvidenceSource.LIVE and not allow_live:
        raise EvidenceValidationError("Live evidence requires an explicit file handoff")
    validate_scenario_evidence(evidence, scenario)  # Before any MCP process starts.
    transport = parse_domain(TransportEvidence, read_json(ROOT / "demo/recorded_transport_evidence.json")
        if transport_payload is None else transport_payload, scenario)
    benefit = parse_domain(TravelBenefitEvidence, read_json(ROOT / "demo/recorded_benefit_evidence.json")
        if benefit_payload is None else benefit_payload, scenario)
    domains = DemoEvidence(evidence, transport, benefit)
    with TemporaryDirectory(prefix="cmu-host-evidence-") as directory, TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        for name, domain in (("host", evidence), ("transport", transport), ("benefit", benefit)):
            (Path(directory) / f"recorded_{name}_evidence.json").write_text(json.dumps(asdict(domain)), encoding="utf-8")
        # Explicit startup sidecar configures the isolated server; no public MCP schema extension.
        if parameters is not None:
            parameters = parameters.model_copy(update={"args": list(parameters.args) + ["--evidence-directory", directory]})
        async with asyncio.timeout(45), Client(stdio_client(parameters or server_parameters(directory), errlog=diagnostics)) as client:
            discovered = (await client.list_tools()).tools
            tools = {tool.name: tool for tool in discovered}
            digest = hashlib.sha256(json.dumps({t.name: [t.input_schema, t.output_schema]
                for t in discovered}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if set(tools) != EXPECTED_TOOLS or len(discovered) != len(EXPECTED_TOOLS) or digest != SCHEMA_DIGEST:
                raise CompatibilityFailure("Existing MCP schema/tool compatibility changed")
            supplemental = await checked_call(client, tools, "get_trip_context", {
                "flight_number": evidence.flight_number, "date": evidence.departure_date})
            context, provenance = evidence_to_context(evidence, supplemental)
            if context["airport_travel_minutes"] != transport.estimated_travel_minutes:
                raise EvidenceValidationError("MCP transport context differs from supplied evidence")
            provenance["airport_travel_minutes"] = transport.source_type
            result = await checked_call(client, tools, "evaluate_trip_plans", {
                "context": context, "candidates": scenario["candidates"]})
            validate_result(result)
            if transport_payload is None and benefit_payload is None:
                validate_baseline(result)
            proposals = propose_calendar_changes(context, result, scenario["calendar_proposal_slots"],
                                                 as_of=scenario["as_of"])
            return {"evidence": evidence, "domains": domains, "context": context, "provenance": provenance,
                    "planning_result": result, "proposals": proposals,
                    "tools": tuple(sorted(tools))}


def render_evidence(report):
    evidence = report["evidence"]
    result = report["planning_result"]
    finalists = result["finalists"]
    live = evidence.source_type == EvidenceSource.LIVE
    return "\n".join(["=== LIVE HOST EVIDENCE HANDOFF ===" if live else "=== HOST EVIDENCE ===",
        f"Source: [{evidence.source_label}]",
        f"Provider: {evidence.provider.value}", f"Flight: {evidence.flight_number}",
        f"Route: {evidence.departure_airport} -> {evidence.arrival_airport}",
        f"Scheduled departure: {evidence.scheduled_departure} ({evidence.time_basis})",
        f"Scheduled arrival: {evidence.scheduled_arrival or 'not supplied'} (retained evidence; not a planner input)",
        f"Evidence timestamp: {evidence.evidence_timestamp or 'not supplied'} ({evidence.time_basis})",
        "Validation: PASSED (structure and demo compatibility; not verified booking authenticity)",
        "Gmail was NOT accessed by this local process. No LLM was called by this local process.",
        "Explicit local file handoff; extraction occurs outside this repository."
            if live else "Recorded sanitized host evidence rehearsal; no live retrieval by Python.",
        "This local stdio connection does not originate inside ChatGPT web.",
        "HOST EVIDENCE VALIDATION: PASSED",
        "MCP TRANSPORT: STDIO",
        "DETERMINISTIC PLANNER: EXECUTED",
        "=== FIELD PROVENANCE [DEMO SIDECAR] ===",
        *[f"{field}: [{source}]" for field, source in report["provenance"].items()],
        "Candidates: [FIXTURE] rehearsed host-style proposals; not authoritative evaluations.",
        "=== DECISION AUTHORITY ===", "HOST SUPPLIES EVIDENCE. PYTHON MAKES THE DECISION.",
        "MCP flow: tools/list -> get_trip_context -> evaluate_trip_plans",
        "Host bridge: validates and maps structured booking fields; retains provenance outside MCP.",
        "MCP Travel Agent: accepts structured context and revalidates it in TravelService.",
        "Deterministic Python: evaluates feasibility and alternatives. No booking ingestion/persistence claimed.",
        render_domains(report["domains"], result),
        "=== RESULT [MCP] ===", f"Planning status: {result['status']}",
        *[f"{'Recommended' if i == 0 else 'Alternative'}: {p['leave_time']} / {p['score']:.2f}" for i, p in enumerate(finalists)],
        *(["No recommendation fabricated. No calendar proposal. CALENDAR WRITE EXECUTED: NO."] if not finalists else []),
        "=== DEMO GOVERNANCE [POST-MCP] ===", render_hitl(report["proposals"]),
        "Source labels describe supplied provenance, not authenticated email or provider authority.",
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Recorded or live host structured booking JSON (explicit local handoff)")
    parser.add_argument("--transport-evidence", type=Path, help="Explicit external normalized transport JSON")
    parser.add_argument("--benefit-evidence", type=Path, help="Explicit external normalized benefit JSON")
    args = parser.parse_args(argv)
    try:
        print(render_evidence(asyncio.run(rehearse_evidence(
            load_payload(args.evidence), allow_live=args.evidence is not None,
            transport_payload=read_json(args.transport_evidence) if args.transport_evidence is not None else None,
            benefit_payload=read_json(args.benefit_evidence) if args.benefit_evidence is not None else None))))
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
