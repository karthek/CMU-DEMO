"""Retrieve and validate a whole snapshot before any repository mutation."""
import json
from travel_agent.itinerary.contracts import IdentityCollision, normalize_record, text, time_basis
from travel_agent.itinerary.models import Diagnostic, NormalizationResult


class ItineraryAgent:
    def __init__(self, source):
        self.source = source
        self.source_id = text(source.source_id)

    def refresh(self):
        try:
            raw = self.source.fetch_snapshot()
        except json.JSONDecodeError:
            return self._failure("REJECTED", "SOURCE_PAYLOAD_INVALID")
        except Exception:
            return self._failure("SOURCE_ERROR", "SOURCE_UNAVAILABLE")
        try:
            time_basis(raw)
            if (set(raw) != {"source_id", "time_basis", "records"}
                    or raw.get("source_id") != self.source_id or not isinstance(raw.get("records"), list)):
                raise ValueError("Invalid snapshot")
        except ValueError:
            count = len(raw["records"]) if isinstance(raw, dict) and isinstance(raw.get("records"), list) else 0
            return self._failure("REJECTED", "SOURCE_PAYLOAD_INVALID", count)
        records, diagnostics, duplicates = {}, [], 0
        for index, item in enumerate(raw["records"]):
            try:
                record, count = normalize_record(item, self.source_id)
                duplicates += count
                if record.itinerary_id in records:
                    if records[record.itinerary_id] != record:
                        raise IdentityCollision("Conflicting itinerary identity")
                    duplicates += 1
                records[record.itinerary_id] = record
            except (ValueError, KeyError, TypeError) as exc:
                code = "IDENTITY_COLLISION" if isinstance(exc, IdentityCollision) else "RECORD_INVALID"
                diagnostics.append(Diagnostic(code, "Itinerary record failed validation.", record_index=index))
        return NormalizationResult(self.source_id, "REJECTED" if diagnostics else "APPLIED",
                                   len(raw["records"]), () if diagnostics else tuple(records[k] for k in sorted(records)),
                                   duplicates, len(diagnostics), tuple(diagnostics))

    def _failure(self, status, code, count=0):
        return NormalizationResult(self.source_id, status, count, (), 0, 0,
                                   (Diagnostic(code, "Itinerary snapshot could not be validated or retrieved."),))
