"""Demo composition of the EXISTING server; unchanged tools and schemas.

Launch with python -B -m demo.mcp_demo_server. Stdout is MCP protocol only.
All runtime state is temporary. No live provider is composed.
"""
import asyncio
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp.server.stdio import stdio_server
from travel_agent.adapters.mcp_server import create_server
from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock, parse_local
from demo.composition import load_demo_evidence, configure_demo_service


async def serve(evidence_directory=None):
    scenario = json.loads(Path(__file__).with_name("scenario.json").read_text(encoding="utf-8"))
    evidence = load_demo_evidence(scenario, evidence_directory)
    with TemporaryDirectory(prefix="cmu-mcp-") as directory:
        root = Path(directory)
        for name in ("itineraries", "flights"):
            (root / f"{name}.json").write_text(json.dumps(scenario[name]), encoding="utf-8")
        service = create_itinerary_service(database_path=root / "demo.sqlite3",
            itinerary_path=root / "itineraries.json", flight_path=root / "flights.json",
            clock=FixedClock(parse_local(scenario["as_of"])))
        try:
            configure_demo_service(service, scenario, evidence)
            server = create_server(service.travel_service, itinerary_service=service)
            async with stdio_server() as streams:
                await server.run(*streams, server.create_initialization_options())
        finally:
            service.repository.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-directory", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.evidence_directory))
