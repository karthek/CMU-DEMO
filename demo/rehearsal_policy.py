"""Demo acceptance checks and failure classes; never compute or replace results."""


class RehearsalFailure(RuntimeError):
    pass


class InfrastructureFailure(RehearsalFailure):
    """Only this category permits automatic transport fallback."""


class AuthoritativeFailure(RehearsalFailure):
    pass


class CompatibilityFailure(RehearsalFailure):
    pass


def validate_result(result):
    """Check returned evidence consistency, without choosing or replacing an answer."""
    import math
    try:
        finalists = result["finalists"]
        if result["status"] == "NO_FEASIBLE_PLAN":
            if finalists or result["selected_plan"] is not None:
                raise ValueError
            return
        if result["status"] != "PLAN_FOUND" or not finalists or result["selected_plan"] != finalists[0]:
            raise ValueError
        for plan in finalists:
            if (not plan["feasibility"]["feasible"] or not math.isfinite(plan["score"])
                or plan["score"] != round(sum(c["value"] for c in plan["score_breakdown"]["components"]), 2)):
                raise ValueError
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise AuthoritativeFailure("Inconsistent authoritative planning result; no fallback") from exc


def validate_baseline(result):
    """Assertions about the frozen demo, not authoritative scoring logic."""
    try:
        validate_result(result)
        if result["status"] != "PLAN_FOUND":
            raise AuthoritativeFailure(f"Authoritative planning status: {result['status']}; no fallback")
        expected = [("2026-09-29T15:30", 97.6), ("2026-09-29T15:20", 96.8)]
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
