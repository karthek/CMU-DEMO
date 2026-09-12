from travel_agent.models import CalendarEvent

class FakeCalendarTool:
    def get_events(self, date: str):
        return [
            CalendarEvent(
                title="Project Review",
                start=f"{date}T14:00",
                end=f"{date}T15:00",
                priority="normal",
            ),
            CalendarEvent(
                title="Leadership Meeting",
                start=f"{date}T16:30",
                end=f"{date}T17:00",
                priority="high",
            ),
        ]
