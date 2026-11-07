"""Client session for the Starling memory service (spec §11, §13.3).

`connect()` opens a session (protocol checked in the handshake) and starts a
background heartbeat thread. `Client.open()` opens a handle on a world and
returns a `RemoteMemory`. The admin methods act on whole worlds.

A session never reconnects or reopens handles by itself: once the service says
`session_expired` (to a call or to a heartbeat), every later call raises
`SessionExpired` and the caller decides whether to `connect()` again. Reopening
silently would hide that another session may have taken the world meanwhile.

Errors never carry the service URL or the token (see errors.py).
"""
from __future__ import annotations

import logging
import threading

from . import errors
from ._transport import PROTOCOL, Transport
from .remote_memory import RemoteMemory
from .types import PendingEvent, from_fields

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/client"


def connect(url: str, *, token: str, timeout_s: float = 30.0) -> "Client":
    """Open a session on the service at `url`.

    Raises `ProtocolMismatch` when the service speaks another protocol version,
    `ServiceUnavailable` / `ServiceTimeout` when it cannot be reached.
    """
    return _open_session(Transport(url, token, timeout_s))


def _open_session(transport: Transport) -> "Client":
    try:
        out = transport.post(f"{_PREFIX}/connect", {"protocol": PROTOCOL})
    except BaseException:
        transport.close()
        raise
    return Client(transport, out)


class Client:
    """One client session. Thread-safe: calls may come from several threads."""

    def __init__(self, transport: Transport, handshake: dict) -> None:
        self._transport = transport
        self._session_id = handshake["session_id"]
        self._lease_ms = int(handshake["lease_ms"])
        self._heartbeat_ms = int(handshake["heartbeat_ms"])
        self._info = handshake.get("info") or {}
        self._lock = threading.Lock()
        self._expired = False
        self._closed = False
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop,
                                           name="starling-client-heartbeat", daemon=True)
        self._heartbeat.start()

    def __repr__(self) -> str:
        return "Client(...)"

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── session plumbing ───────────────────────────────────────────────

    @property
    def timeout_s(self) -> float:
        return self._transport.timeout_s

    @property
    def lease_ms(self) -> int:
        return self._lease_ms

    @property
    def heartbeat_ms(self) -> int:
        return self._heartbeat_ms

    def _check_live(self) -> None:
        with self._lock:
            closed, expired = self._closed, self._expired
        if closed:
            raise errors.MemoryClosed("client is closed")
        if expired:
            raise errors.SessionExpired("session expired; connect again")

    def _mark_expired(self) -> None:
        with self._lock:
            self._expired = True
        self._stop.set()

    def _post(self, path: str, body: dict, *, timeout_s: float | None = None) -> dict:
        """POST to a session-bound route. A `session_expired` answer marks the
        session dead: later calls raise `SessionExpired` without a request."""
        self._check_live()
        try:
            return self._transport.post(f"{_PREFIX}{path}", body, timeout_s=timeout_s)
        except errors.SessionExpired:
            self._mark_expired()
            raise

    def _heartbeat_loop(self) -> None:
        interval = max(self._heartbeat_ms, 1) / 1000.0
        while not self._stop.wait(interval):
            try:
                self._transport.post(f"{_PREFIX}/heartbeat", {"session_id": self._session_id})
            except errors.SessionExpired:
                logger.warning("starling-client heartbeat: session expired")
                self._mark_expired()
                return
            except errors.StarlingClientError as exc:
                # Heartbeat failures are only logged (spec §13.3); the next call reports.
                logger.warning("starling-client heartbeat failed: %s", exc.code)
            except Exception:  # noqa: BLE001 - the heartbeat thread must not die on a surprise
                logger.warning("starling-client heartbeat failed")

    # ── service info and handles ───────────────────────────────────────

    def info(self) -> dict:
        """{version, commit, protocol, extraction_config, default_settings}; no keys or addresses."""
        return self._post("/info", {})

    def open(self, world: str, *, holder: str, read_only: bool = False,
             settings: dict | None = None) -> RemoteMemory:
        """Open a handle on `world`, acting as `holder`.

        `settings`, when given, must equal the world's stored settings (after
        defaults are filled in), else `WorldError` with code
        `world_settings_mismatch`. A writable open while another session holds
        the world raises `WorldError("world_held")`; `world_busy` is transient.
        """
        body = {"session_id": self._session_id, "world": world, "holder": holder,
                "read_only": bool(read_only)}
        if settings is not None:
            body["settings"] = settings
        handle_id = self._post("/open", body)["handle_id"]
        return RemoteMemory(self, handle_id, world=world, holder=holder, read_only=bool(read_only))

    # ── admin operations (whole worlds) ────────────────────────────────

    def _admin(self, op: str, body: dict) -> dict:
        return self._post(f"/admin/{op}", body)

    def create_world(self, name: str, *, labels: dict | None = None, settings: dict | None = None,
                     seed: bool = False) -> dict:
        return self._admin("create_world", {"name": name, "labels": labels, "settings": settings,
                                            "seed": bool(seed)})

    def fork(self, source: str, name: str, *, snapshot_id: str | None = None,
             labels: dict | None = None, retention_group: str | None = None,
             reclaimable: bool = False, seed: bool = False, settings: dict | None = None) -> dict:
        """Fork `source` (or one of its snapshots) into a new world `name`.
        Returns {"world": item, "digest": str}."""
        return self._admin("fork", {"source": source, "name": name, "snapshot_id": snapshot_id,
                                    "labels": labels, "retention_group": retention_group,
                                    "reclaimable": bool(reclaimable), "seed": bool(seed),
                                    "settings": settings})

    def upgrade_seed(self, source: str, name: str) -> dict:
        return self._admin("upgrade_seed", {"source": source, "name": name})

    def snapshot(self, world: str, note: str = "") -> dict:
        """Take a held snapshot (never pruned by retention). Returns {snapshot_id, digest}."""
        return self._admin("snapshot", {"world": world, "note": note})

    def restore(self, world: str, snapshot_id: str) -> str:
        """Restore `world` to `snapshot_id`; returns the restored digest."""
        return self._admin("restore", {"world": world, "snapshot_id": snapshot_id})["digest"]

    def delete_snapshot(self, world: str, snapshot_id: str) -> None:
        self._admin("delete_snapshot", {"world": world, "snapshot_id": snapshot_id})

    def digest(self, world: str) -> str:
        return self._admin("digest", {"world": world})["digest"]

    def pending_events(self, world: str) -> list[PendingEvent]:
        return [from_fields(PendingEvent, row)
                for row in self._admin("pending_events", {"world": world})["events"]]

    def close_world(self, world: str) -> None:
        self._admin("close_world", {"world": world})

    def list_worlds(self, labels: dict | None = None, page: int = 1, page_size: int = 50,
                    with_digest: bool = False) -> dict:
        """One page of worlds: {"worlds": [...], "total", "page", "page_size"}.
        `page` starts at 1. With `with_digest=True` every item carries `digest`."""
        return self._admin("list_worlds", {"labels": labels, "page": int(page),
                                           "page_size": int(page_size),
                                           "with_digest": bool(with_digest)})

    # ── teardown ───────────────────────────────────────────────────────

    def close(self) -> None:
        """Stop the heartbeat, end the session (its handles are released) and
        close the connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            expired = self._expired
        self._stop.set()
        if self._heartbeat is not threading.current_thread():
            self._heartbeat.join()
        try:
            if not expired:
                self._transport.post(f"{_PREFIX}/disconnect", {"session_id": self._session_id})
        except errors.StarlingClientError as exc:
            logger.warning("starling-client disconnect failed: %s", exc.code)
        finally:
            self._transport.close()
