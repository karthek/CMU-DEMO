from travel_agent.models import TransportEstimate


class FakeTransportTool:
    """Return fixed simulated components, not a route or live traffic estimate."""

    def get_estimate(self) -> TransportEstimate:
        return TransportEstimate(
            travel_minutes=45, parking_minutes=10, terminal_walk_minutes=8,
            mode="drive", source="simulated",
        )
