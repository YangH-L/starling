"""Exceptions raised by starling-client. Messages never carry the service URL or token."""
from __future__ import annotations


class StarlingClientError(Exception):
    code: str = ""
    transient: bool = False

    def __init__(self, message: str = "", *, code: str = "", transient: bool | None = None):
        super().__init__(message or code or type(self).__name__)
        if code:
            self.code = code
        if transient is not None:
            self.transient = transient


class RecallDegradedError(StarlingClientError):
    code = "recall_degraded"


class EventConflict(StarlingClientError):
    code = "event_conflict"


class MemoryClosed(StarlingClientError, RuntimeError):
    code = "handle_closed"


class ServiceUnavailable(StarlingClientError):
    code = "service_unavailable"
    transient = True


class ServiceTimeout(StarlingClientError):
    code = "service_timeout"
    transient = True


class ServiceError(StarlingClientError):
    code = "service_error"


class ProtocolMismatch(StarlingClientError):
    code = "protocol_mismatch"


class SessionExpired(StarlingClientError):
    code = "session_expired"


class WorldError(StarlingClientError):
    """Every other server code (world_*, invalid_*, snapshot_*, endpoint_*, ...)."""


class EventNotFound(StarlingClientError, KeyError):
    code = "event_not_found"

    def __str__(self) -> str:  # KeyError.__str__ would repr() the message
        return Exception.__str__(self)


_BY_CODE = {
    "recall_degraded": RecallDegradedError,
    "event_conflict": EventConflict,
    "handle_closed": MemoryClosed,
    "protocol_mismatch": ProtocolMismatch,
    "session_expired": SessionExpired,
    "event_not_found": EventNotFound,
}


def from_body(status: int, body) -> StarlingClientError:
    """Map an HTTP error status and body `{"code","transient","detail"}` to an exception.

    The server owns `transient`. A legacy body `{"detail": code}` has none: 5xx is
    transient, everything else is not.
    """
    if not isinstance(body, dict):
        body = {}
    code = body.get("code") or body.get("detail") or ""
    if not isinstance(code, str):
        code = ""
    transient = body.get("transient")
    if not isinstance(transient, bool):
        transient = status >= 500
    if not code:
        code = f"http_{status}"
    cls = _BY_CODE.get(code)
    if cls is None:
        cls = ServiceError if status >= 500 else WorldError
    return cls(code, code=code, transient=transient)
