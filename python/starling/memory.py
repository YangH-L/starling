"""Starling Memory — public application-surface facade (P2.e).

`Memory.open` builds a local-store SQLite runtime (after relaxing the M0.3
preflight gate). The remember / recall / tick / working-set command logic lives
in `starling._memory_core.MemoryCore` — a single implementation shared with the
dashboard's `DashboardEngine`, so the two surfaces cannot drift. This facade
keeps only construction policy (deterministic stub embedder, llm injected by
the caller) and the dataclass/ContextBlock output shapes.

Offline determinism comes from `make_stub_llm` (`_core.FakeLLMAdapter` with a
canned JSON-array default response — no network). `make_openai_llm` builds the
real `_core.OpenAIAdapter`; its API key is sourced from the environment only
(`OPENAI_API_KEY`), never a function parameter, log line, or hardcoded value.
"""
from __future__ import annotations

import contextlib
import functools
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from starling import _core
from starling import runtime as _runtime
from starling._memory_core import MemoryCore, RecallDegradedError


def make_stub_llm(*, default_response: str, responses: Optional[dict] = None):
    """Offline deterministic LLM adapter.

    `default_response` is a canned JSON array response answering any prompt
    whose hash is not explicitly mapped in `responses` (hash -> canned JSON
    array response). The FakeLLMAdapter ignores the prompt, so tests drive
    behavior purely via the canned response content. Drives `_core.Extractor`
    with zero network calls. Example `default_response`::

        '[{"holder":"self","holder_perspective":"FIRST_PERSON",'
        '"subject":"cog-self","predicate":"responsible_for","object":"auth",'
        '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
    """
    llm = _core.FakeLLMAdapter()
    # binding: set_default_response(raw_xml, ok=True, error="")
    llm.set_default_response(default_response, True, "")
    for h, resp in (responses or {}).items():
        # binding: set_response(hash, raw_xml, ok=True, error="")
        llm.set_response(h, resp, True, "")
    return llm


def make_openai_llm(*, model: str = "gpt-4o-mini", base_url: str = "",
                    timeout_ms: int = 0, max_retries: Optional[int] = None,
                    max_tokens: int = 0):
    """Production LLM adapter (`_core.OpenAIAdapter`).

    The API key is read from the environment (`OPENAI_API_KEY`) by the C++
    adapter — it is never accepted as a parameter, logged, or hardcoded here.
    Starts from `OpenAIAdapterConfig.from_env()` so env-provided defaults
    (base_url / model / timeouts) are honoured, then overrides only the
    fields the caller explicitly set. `max_retries` is judged by `is None`,
    not truthiness: `None` (omitted) keeps the C++ default (3), while any int,
    including 0 ("never retry"), is applied as given.
    """
    cfg = _core.OpenAIAdapterConfig.from_env()
    if model:
        cfg.model = model
    if base_url:
        cfg.base_url = base_url
    if timeout_ms:
        cfg.timeout_ms = timeout_ms
    if max_retries is not None:
        cfg.max_retries = max_retries
    if max_tokens:
        cfg.max_tokens = max_tokens
    return _core.OpenAIAdapter(cfg)


def make_openai_embedder(*, model: str, base_url: str, dim: int,
                         timeout_ms: int, max_retries: Optional[int] = None):
    """Production embedder (`_core.OpenAIEmbeddingAdapter`) for `Memory.open`.

    Built like `make_openai_llm`: the API key is read from the environment
    (`OPENAI_API_KEY`) by the C++ config and is never a parameter. Starts from
    `OpenAIEmbeddingConfig.from_env()`, then sets `model`, `base_url`, `dim`
    and `timeout_ms` unconditionally. `max_retries` is judged by `is None`, not
    truthiness: `None` (omitted) keeps the C++ default (3), while any int,
    including 0 ("never retry"), is applied as given.
    """
    cfg = _core.OpenAIEmbeddingConfig.from_env()
    cfg.model = model
    cfg.base_url = base_url
    cfg.dim = dim
    cfg.timeout_ms = timeout_ms
    if max_retries is not None:
        cfg.max_retries = max_retries
    return _core.OpenAIEmbeddingAdapter(cfg)


def make_anthropic_llm(*, model: str = "", base_url: str = "",
                       timeout_ms: int = 0, max_retries: int = 0,
                       max_tokens: int = 0):
    """Production Anthropic LLM adapter (`_core.AnthropicAdapter`).

    The API key is read from the environment (`ANTHROPIC_API_KEY`) by the C++
    adapter — never accepted as a parameter, logged, or hardcoded here. Starts
    from `AnthropicAdapterConfig.from_env()`, then overrides only the fields the
    caller explicitly set.
    """
    cfg = _core.AnthropicAdapterConfig.from_env()
    if model:
        cfg.model = model
    if base_url:
        cfg.base_url = base_url
    if timeout_ms:
        cfg.timeout_ms = timeout_ms
    if max_retries:
        cfg.max_retries = max_retries
    if max_tokens:
        cfg.max_tokens = max_tokens
    return _core.AnthropicAdapter(cfg)


@dataclass
class RememberResult:
    engram_ref: str
    statement_ids: list = field(default_factory=list)
    outcome: str = ""
    # opus review fix wave (2026-07-12, dashboard ingest worker Task 3): the
    # memory_remember binding now surfaces RememberOutcome.extraction_failed
    # (src/memory/memory_ops.cpp:78) so callers can distinguish a genuine
    # extraction-LLM failure from a legitimate zero-statement extraction —
    # add the field here too so this facade's RememberResult(**dict) forward
    # doesn't reject the new key (same landmine as TickStats/TickOutcome).
    extraction_failed: bool = False
    failure_category: str = ""
    failure_detail: str = ""
    catalog_version: str = ""
    source_preserved: bool = False
    structured_claims_persisted: bool = False
    accepted_by_predicate: dict = field(default_factory=dict)
    rejected_by_predicate: dict = field(default_factory=dict)


@dataclass
class TickStats:
    embedded: int = 0
    fired: int = 0
    broken: int = 0
    auto_withdrawn: int = 0
    # P2.o 周期维护:回放巩固 + 投影兜底 + 出箱收敛(memoryops::tick_all)。
    replay_sampled: int = 0
    consolidated: int = 0
    ttl_archived: int = 0
    projected: int = 0
    dispatched: int = 0
    # P3.c1 Phase 3b: per-stage wall-clock timings — list of {"stage": str, "ms": int}.
    stage_timings_ms: list = field(default_factory=list)
    # P3.c LW.3: stages skipped by load-shedding this tick (same labels as
    # stage_timings_ms); EXCLUDED from the engine idle-heartbeat work-check.
    stages_skipped: list = field(default_factory=list)


@dataclass(frozen=True)
class PendingEvent:
    """One `events` row still in state 'pending': a keyed write that was recorded
    but not yet committed. `seq` is the per-tenant sequence number (starts at 0);
    `failure_category` is '' until a failed attempt records one. `holder` is the
    holder the event belongs to ('' for a pre-0039 row no holder has claimed yet)."""
    key: str
    text: str
    interlocutor: str
    seq: int
    failure_category: str
    holder: str = ""


@dataclass
class EmbedStats:
    """Counts from `Memory.embed_pending`: statements embedded / statements whose
    embedding failed (a later call retries failed rows, up to the retry cap)."""
    embedded: int = 0
    failed: int = 0


class MemoryClosed(RuntimeError):
    """A public `Memory` method was called after `Memory.close()` (or while a
    `close()` is waiting for in-flight calls to return)."""


def _guarded(method):
    """Run a public `Memory` method as one in-flight call (see `Memory.close`)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._in_flight_call():
            return method(self, *args, **kwargs)
    return wrapper


class Memory:
    """Public facade over the Starling write path.

    Thread-safety of the close protocol: every public method counts itself in
    and out; `close()` waits for the count to reach zero before it touches the
    database. The facade starts no threads of its own, so "in flight" means a
    call running on another Python thread (extraction and embedding release the
    GIL inside C++). Concurrent calls are otherwise not serialized here.
    """

    def __init__(self, rt, *, agent: str, tenant_id: str, llm, extraction=None,
                 embedder=None):
        # `_rt` stays addressable: examples/quickstart.py and tests use
        # `mem._rt.adapter` as the single WAL adapter for C++ engine calls.
        self._rt = rt
        self._core = MemoryCore(rt, agent=agent, tenant_id=tenant_id, llm=llm,
                                adapter_name="facade", source_prefix="mem-",
                                extraction=extraction)
        # Default: deterministic stub embedder (no network). A caller-supplied
        # embedder (e.g. `make_openai_embedder(...)`) replaces it wholesale via
        # `set_embedder`, so the writing worker and the reading retriever are
        # built from the same instance.
        self._core.set_embedder(
            _core.StubEmbeddingAdapter(8) if embedder is None else embedder)
        # Close protocol; every field is guarded by `_cond`.
        #   _closed     new calls are rejected with MemoryClosed. Set when a
        #               close() starts; cleared again only if that close() gives
        #               up before tearing anything down (timeout, interrupt).
        #   _closing    some close() is running right now (draining or tearing
        #               down). Other close() calls wait for it.
        #   _torn_down  teardown has started, so `MemoryCore.conn` is (about to
        #               be) None and the handle is (about to be) released: the
        #               facade can no longer be reopened.
        #   _released   teardown ran to the end: WAL merged, handle released.
        # A teardown that fails leaves _closed/_torn_down set and _released
        # clear, with nobody closing: calls keep raising MemoryClosed, and the
        # next close() repeats the (idempotent) teardown.
        self._cond = threading.Condition()
        self._in_flight = 0
        self._closed = False
        self._closing = False
        self._torn_down = False
        self._released = False

    @contextlib.contextmanager
    def _in_flight_call(self):
        with self._cond:
            if self._closed:
                raise MemoryClosed("Memory is closed")
            self._in_flight += 1
        try:
            yield
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()

    @classmethod
    def open(cls, db_path, *, agent: str = "self", tenant_id: str = "default",
             llm=None, extraction=None, embedder=None) -> "Memory":
        # The embedded single-process facade runs the reduced capability set
        # (testing_helper_marker is test-only; engram_per_record_key is deferred
        # to M0.4+KMS). _build_local_store_sqlite_runtime builds the C++
        # RuntimeSupervisor with embedded=True, which waives those deferred caps
        # so preflight reaches READY — no Python-side global relaxation needed.
        rt = _runtime._build_local_store_sqlite_runtime(Path(db_path))
        rt.start()
        return cls(rt, agent=agent, tenant_id=tenant_id, llm=llm,
                   extraction=extraction, embedder=embedder)

    @_guarded
    def remember(self, text: str, *, holder: Optional[str] = None,
                 interlocutor: Optional[str] = None,
                 now: Optional[str] = None) -> RememberResult:
        return RememberResult(**self._core.remember(
            text, holder=holder, interlocutor=interlocutor, now=now))

    @_guarded
    def recall(self, query: str, *, perspective: str = "first_person",
               k: int = 10, mode: str = "semantic", holder: str | None = None,
               strict: bool = False, include_volatile: bool = False) -> list:
        """Retrieve memories by semantic similarity or pattern completion.

        mode="semantic"    — vector cosine search via SemanticRetriever.vector_recall
        mode="completion"  — associative spreading via PatternCompletor.complete

        holder: whose memory to recall (default: this Memory's agent). Must match
        the holder the statements were written under.
        strict: raise RecallDegradedError when the query embedding failed
        (semantic mode) instead of silently returning lexical-only results.
        strict=True with mode="completion" raises ValueError.
        include_volatile: also return statements still in consolidation_state
        'volatile' (embedded via embed_pending but not yet promoted by tick),
        at the cost of possible near-duplicates. Widens only the consolidation
        state; the review guard and holder/perspective predicates are unchanged.
        include_volatile=True with mode="completion" raises ValueError.

        Every hit carries `content_id` alongside `row` and `score`: the row's
        content id (64 lowercase hex chars), which is the same in every library
        holding that statement, unlike `row.id`.
        """
        return self._core.recall(query, perspective=perspective, k=k, mode=mode,
                                 holder=holder, strict=strict,
                                 include_volatile=include_volatile)

    @_guarded
    def query(self, text: str = "", *, intent: str = "FACT_LOOKUP",
              perspective: str | None = None, target: str | None = None,
              subject: str | None = None, predicate: str | None = None,
              k: int = 10, now=None) -> dict:
        """检索规划入口(P3.a1):9 种意图 + Context Pack 8 标签 + 可审计回执。

        返回 dict:entries(row/score/label)、context_pack(LLM-ready 文本)、
        abstained、abstention_reason、receipt(完整回执对象)。
        """
        r = self._core.plan_query(text, intent=intent, perspective=perspective,
                                  target=target, subject=subject,
                                  predicate=predicate, k=k, now=now)
        # 左脑原文条目（形态 S §4.6）：与语句条目并列返回，用 kind 区分。
        # 语句条目补 kind="statement" 保持向后兼容——旧调用方读 row/score/label
        # 不受影响。lane 关闭时 source_entries 为空，结构与本改动前等价。
        return {
            "entries": [{"row": e.row, "score": e.score,
                         "label": e.label.name, "kind": "statement"}
                        for e in r.entries],
            "source_entries": [{"kind": "source",
                                "engram_ref": s.engram_ref,
                                "clause_id": s.clause_id,
                                "speaker": s.speaker,
                                "session_id": s.session_id,
                                "observed_at": s.observed_at,
                                "text": s.text,
                                "provenance": s.provenance,
                                "score": s.score,
                                "rrf_score": s.rrf_score,
                                "rrf_rank": s.rrf_rank}
                               for s in getattr(r, "source_entries", ())],
            "context_pack": r.context_pack,
            "abstained": r.abstained,
            "abstention_reason": r.receipt.abstention_reason,
            "receipt": r.receipt,
        }

    @_guarded
    def latest_event_location(self, theme: str) -> str:
        """Ground-truth current location of `theme` (sub-project A, episodic).

        Returns the location of the highest-seq OCCURRED event about `theme`,
        or "" if no event mentions it. After remembering "Sally puts her ball
        in the basket … Anne moves the ball to the box", this returns "box".
        """
        return self._core.latest_event_location(theme)

    @_guarded
    def tick(self, now: str = "2026-06-01T10:00:00Z") -> TickStats:
        """Advance background workers: embed pending statements + fire due commitments."""
        return TickStats(**self._core.tick(now))

    @_guarded
    def embed_pending(self, now: str) -> EmbedStats:
        """Run only the embedding stage so new statements become recallable.

        Unlike `tick`, this skips commitments, replay/consolidation, projection
        and outbox dispatch, and ignores health gating. Embedding failures are
        counted in `EmbedStats.failed`, not raised.
        """
        return EmbedStats(**self._core.embed_pending(now))

    @_guarded
    def digest(self) -> str:
        """This library's logical digest: 64 lowercase hex characters.

        Two libraries holding the same content give the same digest, and any
        content change gives a different one. Row ids are UUIDs and differ
        between libraries, so the digest normalizes the `statements`,
        `statement_vectors` and `events` rows first — bookkeeping columns
        (counters, wall clocks, batch ids) are dropped, and every id is
        replaced by the content it points at. Every row counts, including rows
        recall cannot see (volatile, pending_review, archived, forgotten).

        An empty library's digest is a fixed constant.
        """
        return self._core.digest()

    @_guarded
    def backup_to(self, path: "str | os.PathLike[str]") -> None:
        """Write a self-contained single-file copy of this library to `path`.

        The copy is made with SQLite's `VACUUM INTO`: one file in rollback-journal
        mode, no `-wal` / `-shm` beside it, so copying that one file is enough to
        carry the whole library. It is first written to `<path>.partial`, checked
        with `PRAGMA integrity_check`, and only then renamed to `path` — `path`
        either does not exist or holds a complete, verified copy. Opening the copy
        with `Memory.open` gives a library whose `digest()` equals this one's.

        The copy holds every tenant in the database file, not just this
        `Memory`'s tenant.

        `path`'s parent directory must already exist; it is not created.

        Raises `FileExistsError` when `path` already exists (it is left
        untouched). Any other failure (a missing parent directory, a copy that
        fails `integrity_check`) raises `RuntimeError`. No `.partial` file is
        left behind on any failure.
        """
        self._core.backup_to(path)

    @_guarded
    def content_ids(self, statement_ids: list[str]) -> dict[str, str]:
        """Map statement UUIDs to their content ids (64 lowercase hex chars).

        The content id is what `recall` reports as a hit's `content_id` and
        what the digest keys rows by: the same statement content gives the same
        content id in every library. An id this library does not hold maps to
        `"missing"`, the same marker the digest uses for a reference it cannot
        resolve, so a caller need not pre-check membership.
        """
        return self._core.content_ids(statement_ids)

    @_guarded
    def next_seq(self) -> int:
        """Next free event sequence number for this tenant.

        0 on an empty library, so the first event's `seq` is 0. Abandoned events
        count, so a number is never reused.
        """
        return self._core.next_seq()

    @_guarded
    def pending_events(self) -> list[PendingEvent]:
        """Events recorded in state 'pending', ordered by `seq` ascending."""
        return [PendingEvent(**row) for row in self._core.pending_events()]

    @_guarded
    def remember_event(self, key: str, text: str, *, interlocutor: str,
                       epoch: str = "2026-01-01T00:00:00Z") -> RememberResult:
        """Remember `text` once under the caller's stable `key`, idempotently.

        A retry with the same key and the same text never extracts twice and
        never writes duplicate statements: a committed receipt returns the
        stored statement ids without calling the llm, and a crash between the
        statement write and the receipt update converges on the next call. The
        key is written into the remembered text as a `[key] ` prefix.

        `epoch` is the clock origin — the engram's `created_at` is `epoch` plus
        the event's `seq` seconds, so a retry keeps the timestamp it first got.

        Raises `EventConflict` when the key already records a different text,
        when it was abandoned, or when a still-pending event is retried with a
        different `interlocutor` than the one it was first recorded with (the
        message says which). A committed event is returned as stored without
        extracting, so its `interlocutor` is not compared.
        """
        return RememberResult(**self._core.remember_event(
            key, text, interlocutor=interlocutor, epoch=epoch))

    @_guarded
    def abandon_event(self, key: str) -> None:
        """Abandon the event recorded under `key`: it will never be written.

        Only the receipt row changes (state 'pending' -> 'abandoned'); nothing
        else is touched, and the event's `seq` is not reused. Abandoning is
        final: a later `remember_event` on this key raises `EventConflict`,
        whatever the text. Abandoning an already abandoned key is a no-op.

        Raises `EventConflict` when the event is already committed (its
        statements are stored and cannot be taken back), and `KeyError` when no
        event was ever recorded under `key`.
        """
        self._core.abandon_event(key)

    @_guarded
    def render_working_set(self, interlocutor, *, goal=None, token_budget: int = 2000):
        """Assemble a prompt-ready ContextBlock from memory (P2.e).

        Five sections — persona / common_ground / relevant_memories /
        pending_commitments / affect — under an approximate token budget. A
        `fired` commitment surfaces as a ⚠ DUE reminder (B3 closure).
        """
        return self._core.build_working_set(interlocutor, goal=goal,
                                            token_budget=token_budget)

    def close(self, timeout: float | None = None) -> None:
        """Wait for in-flight calls to return, then close the database.

        From the moment `close` starts, new calls raise `MemoryClosed`. Once the
        in-flight calls have returned, the WAL is merged into the main file
        (`PRAGMA wal_checkpoint(TRUNCATE)`) and the connection is closed, so the
        library is one self-contained file. Afterwards every public method
        raises `MemoryClosed`, and a further `close()` does nothing.

        `timeout` (seconds, default: wait as long as it takes) bounds the wait.
        `TimeoutError` means one of two different things, told apart by its
        message:

        * "... call(s) still in flight": this `close()` gave up waiting for
          in-flight calls. Nothing was torn down; the library stays open, the
          facade accepts calls again, and `close()` can be retried.
        * "another close() is in progress": a different `close()` is already
          closing the library and this one gave up waiting for it. Calls raise
          `MemoryClosed` for as long as that `close()` runs. It usually ends in
          a closed library, but if it is still draining in-flight calls and
          then times out itself, the library reopens.

        If closing itself fails after the in-flight calls have returned, the
        exception propagates and the library stays closed to new calls, and
        calling `close()` again repeats the teardown (both steps are
        idempotent). Whether that retry still merges the WAL depends on where
        the failure was: `MemoryCore.close()` raising, or an interrupt between
        the two steps, leaves the handle open, so the retry does merge it. A
        raising checkpoint does not, because `SqliteAdapter::close()` releases
        the handle before rethrowing — the retry then early-returns and the
        WAL is left on disk.

        Closing is not a write, so it takes no write gate. Calling `close` from
        inside one of this `Memory`'s own calls (for instance from an llm
        callback) waits for itself; pass a `timeout` there.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        # One try/finally covers everything from the moment this call becomes
        # the closer, so an interrupt (KeyboardInterrupt) at any point cannot
        # leave `_closing` set with nobody closing.
        claimed = False
        finished = False
        try:
            with self._cond:
                while True:
                    if self._released:
                        return
                    if not self._closing:
                        break
                    # Another close() is draining or tearing down: wait for it
                    # to finish (released) or to give up / fail (not closing).
                    if not self._cond.wait_for(
                            lambda: self._released or not self._closing, remaining()):
                        raise TimeoutError(
                            f"Memory.close: another close() is in progress and did "
                            f"not finish within {timeout}s; the library is closing "
                            f"or already closed")
                claimed = True
                self._closing = True
                self._closed = True
                if not self._cond.wait_for(lambda: self._in_flight == 0, remaining()):
                    raise TimeoutError(
                        f"Memory.close: {self._in_flight} call(s) still in flight "
                        f"after {timeout}s; the library stays open")
                self._torn_down = True
            # No call is in flight and new ones are rejected, so nothing touches
            # the connection from here on. Order matters: `_core.close()` drops
            # its `conn` reference before the adapter releases the handle behind
            # it. Both steps are idempotent, which is what makes a retry after a
            # failure safe. `finished` is set only when both ran to the end: if
            # `_core.close()` raised or the call was interrupted before
            # `adapter.close()`, the handle is still open and the WAL unmerged.
            self._core.close()
            self._rt.adapter.close()
            finished = True
        finally:
            if claimed:
                with self._cond:
                    self._closing = False
                    if finished:
                        self._released = True
                    elif not self._torn_down:
                        # Gave up before tearing anything down (timeout or
                        # interrupt while draining): serve calls again.
                        self._closed = False
                    self._cond.notify_all()
