"""Stable agent failures. Serialize to_dict(), never exception tracebacks/causes."""


class TravelAgentError(Exception):
    code = "AGENT_ERROR"

    def __init__(self, message: str, *, agent: str):
        self.message = message
        self.agent = agent
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "agent": self.agent}


class InvalidInputError(TravelAgentError):
    code = "INVALID_INPUT"


class DataUnavailableError(TravelAgentError):
    code = "DATA_UNAVAILABLE"


class InvalidDataError(TravelAgentError):
    code = "INVALID_DATA"


class ToolFailureError(TravelAgentError):
    code = "TOOL_FAILURE"
