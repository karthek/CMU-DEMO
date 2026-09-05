from travel_agent.models import FlightState

class FakeFlightTool:
    def get_flight(self, flight_number: str, date: str):
        return FlightState(
            flight_number=flight_number,
            origin="ATL",
            destination="PHL",
            departure_time=f"{date}T19:00",
            boarding_time=f"{date}T18:30",
            status="ON TIME",
            gate="B18",
            delay_minutes=0,
        )
