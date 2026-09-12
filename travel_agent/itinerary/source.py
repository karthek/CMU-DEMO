import json
from pathlib import Path
from typing import Protocol


class ItinerarySource(Protocol):
    source_id: str

    def fetch_snapshot(self) -> dict: ...


class FixtureItinerarySource:
    def __init__(self, path, source_id="fixture"):
        self.path = Path(path)
        self.source_id = source_id

    def fetch_snapshot(self):
        return json.loads(self.path.read_text(encoding="utf-8"))
