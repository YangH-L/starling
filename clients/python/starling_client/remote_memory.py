"""RemoteMemory: one handle on one world, acting as one holder (spec §11, §13.3).

The method signatures and return shapes follow `starling.Memory` where the two
overlap. Calls go to `/api/v1/client/call` with the handle id; the service runs
them on the world's engine under the handle's holder.

Close protocol (same meaning as `Memory.close`): every call counts itself in
and out locally; `close(timeout)` first waits for the local count to reach
zero, then asks the service, which waits for the handle's in-flight calls on
its side. A timeout on either side raises `TimeoutError` and the handle stays
open: calls are accepted again and `close()` can be retried. A second
`close()` while one is running waits for it (up to its own timeout), and
`timeout=None` waits as long as it takes, asking the service in bounded
segments.
"""
from __future__ import annotations

import contextlib
import threading
import time

from . import errors
from .types import EmbedStats, PendingEvent, RememberResult, StatementRow, from_fields

# close() 每次请求服务端最多等多久（秒）。timeout=None 时按这一段反复问，直到服务端的在途调用归零；每次 HTTP 请求的超时是这一段
# 加上客户端的请求超时，所以不会因为服务端在等而先超时（最后修复波 A8）。
_CLOSE_SEGMENT_S = 25.0


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

    def content_ids(self, statement_ids) -> dict[str, str]:
        """Map statement ids to content ids, like `Memory.content_ids`; an id the
        world does not hold maps to "missing"."""
        return self._call("content_ids", ids=[str(item) for item in statement_ids])

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
        handle. Same meaning as `Memory.close`:

        * New calls raise `MemoryClosed` from the moment close starts.
        * `timeout=None` waits as long as it takes. The service is asked in
          bounded segments (each request waits at most `_CLOSE_SEGMENT_S` on the
          service side), so no single HTTP request hits the client timeout.
        * `TimeoutError` "... still in flight": a call was still in flight after
          `timeout` seconds. The handle stays open and accepts calls again;
          `close()` can be retried.
        * `TimeoutError` "another close() is in progress": a different `close()`
          is running and did not finish within this call's `timeout`. A second
          `close()` waits for the first one; once that one has closed the handle,
          the second returns. If the first gives up, the second takes over.
        * Idempotent once closed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        with self._cond:
            while True:
                if self._closed:
                    return
                if not self._closing:
                    break
                if not self._cond.wait_for(lambda: self._closed or not self._closing, remaining()):
                    raise TimeoutError(f"another close() is in progress and did not finish within "
                                       f"{timeout}s; the handle is closing or already closed")
            self._closing = True
        try:
            with self._cond:
                if not self._cond.wait_for(lambda: self._in_flight == 0, remaining()):
                    raise TimeoutError(f"{self._in_flight} call(s) still in flight after {timeout}s;"
                                       " the handle stays open")
            self._close_on_service(deadline)
            with self._cond:
                self._closed = True
        finally:
            with self._cond:
                self._closing = False
                self._cond.notify_all()

    def _close_on_service(self, deadline: float | None) -> None:
        """Ask the service to close the handle once its in-flight calls are done. Each request waits at
        most one segment on the service side (the whole remaining time when that is shorter); `world_busy`
        means "still in flight", so with no deadline it asks again, and past the deadline it raises
        `TimeoutError` (the handle stays open)."""
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            server_wait = _CLOSE_SEGMENT_S if left is None else min(left, _CLOSE_SEGMENT_S)
            # The HTTP request may take as long as the service-side wait, plus the usual request budget.
            http_timeout = server_wait + self._client.timeout_s
            busy = False
            try:
                self._client._post("/close", {"handle_id": self._handle_id, "timeout_s": server_wait},
                                   timeout_s=http_timeout)
            except errors.StarlingClientError as exc:
                if exc.code != "world_busy":
                    raise
                busy = True
            if not busy:
                return
            if deadline is not None and time.monotonic() >= deadline:
                # 块外再抛：TimeoutError 不挂服务端错误的链（F5）
                raise TimeoutError("call(s) still in flight on the service; the handle stays open")
