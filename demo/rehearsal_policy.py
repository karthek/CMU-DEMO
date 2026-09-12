"""Demo acceptance checks and failure classes; never compute or replace results."""


class RehearsalFailure(RuntimeError):
    pass


class InfrastructureFailure(RehearsalFailure):
    """Only this category permits automatic transport fallback."""


class AuthoritativeFailure(RehearsalFailure):
    pass


class CompatibilityFailure(RehearsalFailure):
    pass


def validate_baseline(result):
    """Assertions about the frozen demo, not authoritative scoring logic."""
    try:
        if result["status"] != "PLAN_FOUND":
            raise AuthoritativeFailure(f"Authoritative planning status: {result['status']}; no fallback")
        expected = [("2026-09-16T16:20", 76.8), ("2026-09-16T16:10", 76.0)]
        selected = result["selected_plan"]
        if ([(p["leave_time"], p["score"]) for p in result["finalists"]] != expected
            or selected != result["finalists"][0]
            or not selected["feasibility"]["feasible"]
            or [(c["title"], c["priority"], c["conflict_type"], c["penalty"])
                for c in selected["calendar_conflicts"]] != [
                    ("Leadership Meeting", "high", "DEPARTURE_BEFORE_OR_AT_EVENT_START", -20.0)]):
            raise AuthoritativeFailure("Authoritative result differs from the approved demo baseline; no fallback")
    except (KeyError, TypeError, IndexError) as exc:
        raise AuthoritativeFailure("Incomplete authoritative baseline evidence; no fallback") from exc
