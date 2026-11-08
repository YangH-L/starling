"""starling-client: pure-Python client for the Starling memory service."""
from ._transport import PROTOCOL, Transport
from .client import Client, connect
from .errors import (
    EventConflict, EventNotFound, MemoryClosed, ProtocolMismatch, RecallDegradedError,
    ServiceError, ServiceTimeout, ServiceUnavailable, SessionExpired, StarlingClientError,
    Unauthorized, WorldError,
)
from .remote_memory import RemoteMemory
from .types import EmbedStats, PendingEvent, RememberResult, StatementRow

try:
    from ._build_info import COMMIT, VERSION
except ImportError:  # source checkout without a build
    COMMIT, VERSION = "unknown", "unknown"

__all__ = [
    "connect", "Client", "RemoteMemory", "PROTOCOL", "Transport", "COMMIT", "VERSION", "EmbedStats", "PendingEvent",
    "RememberResult", "StatementRow", "StarlingClientError", "RecallDegradedError",
    "EventConflict", "EventNotFound", "MemoryClosed", "ProtocolMismatch", "ServiceError",
    "ServiceTimeout", "ServiceUnavailable", "SessionExpired", "Unauthorized", "WorldError",
]
