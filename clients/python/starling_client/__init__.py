"""starling-client: pure-Python client for the Starling memory service."""
from ._transport import PROTOCOL, Transport
from .errors import (
    EventConflict, EventNotFound, MemoryClosed, ProtocolMismatch, RecallDegradedError,
    ServiceError, ServiceTimeout, ServiceUnavailable, SessionExpired, StarlingClientError,
    WorldError,
)
from .types import EmbedStats, PendingEvent, RememberResult, StatementRow

try:
    from ._build_info import COMMIT, VERSION
except ImportError:  # source checkout without a build
    COMMIT, VERSION = "unknown", "unknown"

__all__ = [
    "PROTOCOL", "Transport", "COMMIT", "VERSION", "EmbedStats", "PendingEvent",
    "RememberResult", "StatementRow", "StarlingClientError", "RecallDegradedError",
    "EventConflict", "EventNotFound", "MemoryClosed", "ProtocolMismatch", "ServiceError",
    "ServiceTimeout", "ServiceUnavailable", "SessionExpired", "WorldError",
]
