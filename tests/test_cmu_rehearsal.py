"""CP5 orchestration, explicit fallback authorization, and presentation parity."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from demo.cmu_demo import run_demo
from demo import run_cmu_demo as launcher
from demo import mcp_host_demo as host
from demo.rehearsal_policy import (InfrastructureFailure, AuthoritativeFailure,
                                   CompatibilityFailure, validate_baseline)
from travel_agent.adapters.mcp_server import TOOLS


def remote_report(direct):
    run = direct["booked_result"]["run"]
    return {"planning_result": run["planning_result"], "booked_result": direct["booked_result"],
            "proposals": direct["calendar_proposals"]}


def test_mcp_first_and_full_parity():
    direct = run_demo()
    order = []
    def mcp():
        order.append("mcp")
        return remote_report(direct)
    def local():
        order.append("trace replay")
        return run_demo()
    report = launcher.rehearse_demo(mcp_runner=mcp, direct_runner=local)
    assert order == ["mcp", "trace replay"]
    assert report["demo"] == direct
    text = launcher.render_rehearsal(report)
    assert "MODE: MCP / LOCAL STDIO" in text
    assert "Status: CONNECTED" in text
    assert "parity-checked LOCAL production execution replay" in text
    assert "Leave: 16:20 | Score: 76.80" in text
    assert "Alternative: 16:10 / 76.00" in text


def test_infrastructure_fallback_uses_real_direct_path_and_sanitizes_reason():
    runner = Mock(side_effect=InfrastructureFailure("secret-token / private/path"))
    direct = Mock(wraps=run_demo)
    report = launcher.rehearse_demo(mcp_runner=runner, direct_runner=direct)
    runner.assert_called_once()
    direct.assert_called_once()
    assert report["demo"] == run_demo()
    text = launcher.render_rehearsal(report)
    assert "MODE: OFFLINE FIXTURE FALLBACK" in text
    assert "MCP host: UNAVAILABLE" in text and "Fallback: ENABLED" in text
    assert "secret-token" not in text and "private/path" not in text
    assert "Status: CONNECTED" not in text


@pytest.mark.parametrize("failure", [AuthoritativeFailure("planning validation failed"),
    AuthoritativeFailure("NO_FEASIBLE_PLAN"), CompatibilityFailure("schema changed"), RuntimeError("unknown")])
def test_non_infrastructure_errors_never_fallback(failure):
    local = Mock()
    with pytest.raises(type(failure)):
        launcher.rehearse_demo(mcp_runner=Mock(side_effect=failure), direct_runner=local)
    local.assert_not_called()


@pytest.mark.parametrize("status", ["NO_FEASIBLE_PLAN", "WRONG_SCORE"])
def test_bad_authoritative_payload_never_fallback(status):
    remote = deepcopy(remote_report(run_demo()))
    if status == "NO_FEASIBLE_PLAN":
        remote["planning_result"]["status"] = status
    else:
        remote["planning_result"]["finalists"][0]["score"] = 999
    local = Mock()
    with pytest.raises(AuthoritativeFailure):
        launcher.rehearse_demo(mcp_runner=lambda: remote, direct_runner=local)
    local.assert_not_called()


def test_no_fallback_and_explicit_offline():
    mcp = Mock(side_effect=InfrastructureFailure("unavailable"))
    local = Mock(wraps=run_demo)
    with pytest.raises(InfrastructureFailure):
        launcher.rehearse_demo(fallback=False, mcp_runner=mcp, direct_runner=local)
    local.assert_not_called()
    mcp.reset_mock()
    first = launcher.rehearse_demo(mode="offline", mcp_runner=mcp)
    second = launcher.rehearse_demo(mode="offline", mcp_runner=mcp)
    mcp.assert_not_called()
    assert first == second
    assert launcher.render_rehearsal(first) == launcher.render_rehearsal(second)
    assert "NOT ATTEMPTED" in launcher.render_rehearsal(first)


def test_direct_failure_stops_without_fabrication(capsys):
    with patch.object(launcher, "run_demo", side_effect=RuntimeError("secret")):
        assert launcher.main(["--mode", "offline"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "demo stopped without an answer" in captured.err
    assert "secret" not in captured.err and "Traceback" not in captured.err


def test_cli_no_fallback_and_too_late(capsys):
    with patch.object(launcher, "run_mcp", side_effect=InfrastructureFailure("unavailable")):
        assert launcher.main(["--no-fallback"]) == 1
    assert "INFRASTRUCTURE FAILURE" in capsys.readouterr().err
    assert launcher.main(["--mode", "offline", "--too-late"]) == 0
    assert "AUTHORITATIVE RESULT: NO_FEASIBLE_PLAN" in capsys.readouterr().out


@pytest.mark.parametrize("fault,expected", [("missing_tool", InfrastructureFailure),
    ("schema", CompatibilityFailure), ("tool_error", AuthoritativeFailure),
    ("invalid_response", InfrastructureFailure), ("no_plan", AuthoritativeFailure)])
def test_actual_host_failure_classification(fault, expected):
    direct = run_demo()
    remote = remote_report(direct)
    tools = deepcopy(list(TOOLS))
    if fault == "missing_tool":
        tools.pop()
    if fault == "schema":
        tools[0].input_schema["description"] = "Unexpected schema change"
    async def call(name, args):
        payload = direct["booked_result"]["run"]["context"] if name == "get_trip_context" else remote["planning_result"]
        if fault == "invalid_response":
            payload = None
        if fault == "no_plan" and name == "evaluate_trip_plans":
            payload = dict(payload, status="NO_FEASIBLE_PLAN", selected_plan=None, finalists=[])
        return SimpleNamespace(is_error=fault == "tool_error", structured_content=payload)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.list_tools.return_value = SimpleNamespace(tools=tools)
    client.call_tool.side_effect = call
    with patch.object(host, "Client", return_value=client), pytest.raises(expected):
        asyncio.run(host.rehearse())


def test_wrapped_business_error_is_not_reclassified_as_infrastructure():
    client = AsyncMock()
    client.__aenter__.side_effect = ExceptionGroup("transport cleanup", [
        InfrastructureFailure("transport"), AuthoritativeFailure("planning refused")])
    with patch.object(host, "Client", return_value=client), pytest.raises(AuthoritativeFailure):
        asyncio.run(host.rehearse())


def test_two_real_mcp_rehearsals_and_governance():
    first, second = launcher.rehearse_demo(fallback=False), launcher.rehearse_demo(fallback=False)
    assert first == second
    assert launcher.render_rehearsal(first) == launcher.render_rehearsal(second)
    direct = first["demo"]
    validate_baseline(direct["booked_result"]["run"]["planning_result"])
    assert (direct["beam_width"], direct["depth"]) == (2, 3)
    proposal = direct["calendar_proposals"][0]
    assert proposal.proposed_start == "2026-09-16T15:15:00"
    assert proposal.proposed_end == "2026-09-16T15:45:00"
    assert proposal.approval_status == "AWAITING_USER_APPROVAL"
    assert proposal.execution_status == "NOT_EXECUTED"
    assert first["demo"] == launcher.rehearse_demo(mode="offline")["demo"]
