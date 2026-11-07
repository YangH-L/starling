"""RemoteMemory: one handle on one world, acting as one holder (spec §11, §13.3).

The method signatures and return shapes follow `starling.Memory` where the two
overlap. Calls go to `/api/v1/client/call` with the handle id; the service runs
them on the world's engine under the handle's holder.

Close protocol (same meaning as `Memory.close`): every call counts itself in
and out locally; `close(timeout)` first waits for the local count to reach
zero, then asks the service, which waits for the handle's in-flight calls on
its side. A timeout on either side raises `TimeoutError` and the handle stays
open: calls are accepted again and `close()` can be retried.
"""
from __future__ import annotations

import contextlib
import threading
import time

from . import errors
from .types import EmbedStats, PendingEvent, RememberResult, StatementRow, from_fields


class RemoteMemory:
    def __init__(self, client, handle_id: str, *, world: str, holder: str,
                 read_only: bool) -> None:
        self._client = client
        self._handle_id = handle_id
        self.world = world
        self.holder = holder
        self.read_only = read_only
        self._cond = threading.Condition()
        self._in_flight = 0
        self._closing = False
        self._closed = False

    def __repr__(self) -> str:
        return f"RemoteMemory(world={self.world!r}, holder={self.holder!r})"

    def __enter__(self) -> "RemoteMemory":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @contextlib.contextmanager
    def _in_flight_call(self):
        with self._cond:
            if self._closed or self._closing:
                raise errors.MemoryClosed("handle is closed")
            self._in_flight += 1
        try:
            yield
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()

    def _call(self, method: str, **args):
        with self._in_flight_call():
            out = self._client._post("/call", {"handle_id": self._handle_id, "method": method,
                                               "args": args})
        return out.get("result")

    # ── as the handle's holder ─────────────────────────────────────────

    def recall(self, query: str, *, k: int = 10, mode: str = "semantic", strict: bool = True,
               include_volatile: bool = True) -> list[dict]:
        """Each hit is {"content_id", "score", "row"}; `row` is a frozen StatementRow.
        `strict=True` raises `RecallDegradedError` when the query embedding failed."""
        hits = self._call("recall", query=query, k=int(k), mode=mode, strict=bool(strict),
                          include_volatile=bool(include_volatile))
        return [{"content_id": hit["content_id"], "score": hit["score"],
                 "row": from_fields(StatementRow, hit["row"])} for hit in hits]

    def remember_event(self, key: str, text: str, *, interlocutor: str,
                       epoch: str = "2026-01-01T00:00:00Z") -> RememberResult:
        """Remember `text` once under `key` (idempotent). Raises `EventConflict`
        like `Memory.remember_event`."""
        return from_fields(RememberResult, self._call("remember_event", key=key, text=text,
                                                      interlocutor=interlocutor, epoch=epoch))

    def abandon_event(self, key: str) -> None:
        """Abandon this holder's pending event `key`. Raises `KeyError`
        (`EventNotFound`) when this holder has no such event, `EventConflict`
        when it is already committed."""
        self._call("abandon_event", key=key)

    def content_ids(self, ids) -> dict[str, str]:
        return self._call("content_ids", ids=[str(item) for item in ids])

    # ── the whole world ────────────────────────────────────────────────

    def digest(self) -> str:
        return self._call("digest")

    def pending_events(self) -> list[PendingEvent]:
        return [from_fields(PendingEvent, row) for row in self._call("pending_events")]

    def next_seq(self) -> int:
        return self._call("next_seq")

    def embed_pending(self, now: str) -> EmbedStats:
        return from_fields(EmbedStats, self._call("embed_pending", now=now))

    # ── close ──────────────────────────────────────────────────────────

    def close(self, timeout: float | None = None) -> None:
        """Wait for in-flight calls (locally, then on the service), then close the
        handle. New calls raise `MemoryClosed` from the moment close starts.
        `TimeoutError` means a call was still in flight after `timeout` seconds:
        the handle stays open and accepts calls again. Idempotent once closed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if self._closed:
                return
            if self._closing:
                raise TimeoutError("another close() is in progress")
            self._closing = True
        try:
            with self._cond:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if not self._cond.wait_for(lambda: self._in_flight == 0, remaining):
                    raise TimeoutError(f"{self._in_flight} call(s) still in flight after {timeout}s;"
                                       " the handle stays open")
            server_wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            body = {"handle_id": self._handle_id}
            if server_wait is not None:
                body["timeout_s"] = server_wait
            # The HTTP request may take as long as the service-side wait, plus the usual request budget.
            http_timeout = None if server_wait is None else server_wait + self._client.timeout_s
            busy = False
            try:
                self._client._post("/close", body, timeout_s=http_timeout)
            except errors.StarlingClientError as exc:
                if exc.code != "world_busy":
                    raise
                busy = True
            if busy:   # 块外再抛：TimeoutError 不挂服务端错误的链（F5）
                raise TimeoutError("call(s) still in flight on the service; the handle stays open")
            with self._cond:
                self._closed = True
        finally:
            with self._cond:
                self._closing = False
                self._cond.notify_all()
