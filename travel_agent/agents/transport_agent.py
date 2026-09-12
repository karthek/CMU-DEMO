from functools import partial
from travel_agent.agents import _validation
from travel_agent.agents.errors import DataUnavailableError, InvalidDataError, ToolFailureError
from travel_agent.models import TransportEstimate

duration = partial(_validation.duration, agent="transport")
text = partial(_validation.text, agent="transport")


class TransportAgent:
    """Validate separate transport durations; never score departure plans."""

    def __init__(self, transport_tool):
        self._transport_tool = transport_tool

    def get_estimate(self) -> TransportEstimate:
        try:
            estimate = self._transport_tool.get_estimate()
        except Exception as exc:
            raise ToolFailureError("Transport retrieval failed.", agent="transport") from exc
        if estimate is None:
            raise DataUnavailableError("Transport estimate is unavailable.", agent="transport")
        if not isinstance(estimate, TransportEstimate):
            raise InvalidDataError("Transport data has an invalid structure.", agent="transport")
        return TransportEstimate(
            travel_minutes=duration(estimate.travel_minutes, "travel_minutes"),
            parking_minutes=duration(estimate.parking_minutes, "parking_minutes"),
            terminal_walk_minutes=duration(estimate.terminal_walk_minutes, "terminal_walk_minutes"),
            mode=text(estimate.mode, "mode"), source=text(estimate.source, "source"),
        )
