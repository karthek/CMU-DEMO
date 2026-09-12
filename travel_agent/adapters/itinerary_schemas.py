"""Closed V8 wire contracts. Existing V7 schemas are referenced unchanged."""


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def enum(*values):
    return {"enum": list(values)}


def array(item):
    return {"type": "array", "items": item}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


TEXT = {"type": "string"}
COUNT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
LOCAL = {"type": "string", "description": "America/New_York local ISO date-time without timezone."}
STATE = enum("NOT_ACTIVATED", "PLANNING", "COMPLETED", "FAILED")
DIAGNOSTIC = obj(dict(code=TEXT, message=TEXT, record_index=nullable(COUNT), itinerary_id=nullable(TEXT),
                      segment_id=nullable(TEXT), field=nullable(TEXT)))
SEGMENT = obj(dict(itinerary_id=TEXT, segment_id=TEXT, source_segment_id=TEXT, flight_number=TEXT,
                  departure_date=TEXT, origin=TEXT, destination=TEXT, scheduled_departure=LOCAL,
                  booking_status=enum("CONFIRMED", "CANCELLED")))
ACTIVATION = obj(dict(itinerary_id=nullable(TEXT), segment_id=nullable(TEXT),
                     status=enum("ELIGIBLE", "NOT_YET_ELIGIBLE", "DEPARTED", "CANCELLED", "INVALID_SEGMENT"),
                     eligible=BOOL, as_of=LOCAL, scheduled_departure=nullable(LOCAL), activation_time=nullable(LOCAL),
                     planning_lead_time_minutes=COUNT, time_until_departure_minutes=nullable({"type": "number"}),
                     reason_code=enum("WITHIN_PLANNING_WINDOW", "BEFORE_PLANNING_WINDOW", "DEPARTURE_REACHED",
                                      "BOOKING_CANCELLED", "SEGMENT_VALIDATION_FAILED"),
                     evidence=obj(dict(rule=enum("AUTOMATIC_PLANNING_WINDOW_V1"), lower_bound_inclusive=enum(True),
                                       upper_bound_exclusive=enum(True), at_or_after_activation=nullable(BOOL),
                                       before_departure=nullable(BOOL), booking_status=nullable(enum("CONFIRMED", "CANCELLED")),
                                       validation_codes=array(TEXT)))))
REFRESH = obj(dict(status=enum("APPLIED", "REJECTED", "SOURCE_ERROR"), source_id=TEXT, as_of=LOCAL,
                   records_retrieved=COUNT, itineraries_created=COUNT, itineraries_updated=COUNT,
                   itineraries_unchanged=COUNT, duplicates_ignored=COUNT, invalid_records=COUNT,
                   itineraries_marked_missing=COUNT, upcoming_segments=array(SEGMENT), diagnostics=array(DIAGNOSTIC)))
PROVENANCE = obj(dict(activation_mode=enum("AUTOMATIC", "USER_INITIATED"), trigger=enum("AUTOMATIC_LEAD_TIME", "USER_REQUEST"),
                      as_of=LOCAL, itinerary_id=TEXT, segment_id=TEXT, itinerary_revision=COUNT))
MONITOR_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
SELECTOR = {"type": "object", "properties": {
    "itinerary_id": {"type": "string", "minLength": 1}, "segment_id": {"type": "string", "minLength": 1},
    "destination": {"type": "string", "pattern": "^[A-Z]{3}$"},
    "departure_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}},
    "dependentRequired": {"segment_id": ["itinerary_id"]}, "additionalProperties": False}


def schemas(candidate_schema, context_schema, planning_schema):
    manual_input = {"type": "object", "properties": {"selector": SELECTOR,
                    "candidates": {"anyOf": [{"type": "array", "items": candidate_schema, "minItems": 1},
                                              {"type": "null"}], "default": None}},
                    "required": ["selector"], "additionalProperties": False}
    attempt = obj(dict(attempt_number=COUNT, activation_mode=enum("AUTOMATIC", "USER_INITIATED"),
                       trigger=enum("AUTOMATIC_LEAD_TIME", "USER_REQUEST"), itinerary_revision=COUNT,
                       decision_as_of=LOCAL, started_at=LOCAL, finished_at=nullable(LOCAL),
                       state=enum("PLANNING", "COMPLETED", "FAILED"), activation_result=ACTIVATION,
                       provenance=PROVENANCE, context=nullable(context_schema), planning_result=nullable(planning_schema),
                       error=nullable(DIAGNOSTIC)))
    dispatch = obj(dict(itinerary_id=TEXT, segment_id=TEXT,
                        decision=enum("NOT_ELIGIBLE", "PLAN", "RETRY", "SUPPRESS_DUPLICATE"),
                        automatic_state_before=STATE, automatic_state_after=STATE,
                        current_itinerary_revision=COUNT, activation_result=ACTIVATION, attempt=nullable(attempt)))
    monitor = obj(dict(as_of=LOCAL, time_basis=enum("America/New_York"), refresh=nullable(REFRESH),
                       activation_results=array(ACTIVATION), dispatches=array(dispatch),
                       activated_segments=array(SEGMENT), diagnostics=array(DIAGNOSTIC)))
    manual = obj(dict(as_of=LOCAL, time_basis=enum("America/New_York"), refresh=nullable(REFRESH),
                      status=enum("COMPLETED", "FAILED", "NOT_FOUND", "NEEDS_SELECTION", "REFRESH_BLOCKED", "BUSY"),
                      matches=array(SEGMENT), run=nullable(attempt), diagnostics=array(DIAGNOSTIC)))
    return manual_input, monitor, manual
