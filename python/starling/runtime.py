"""Runtime supervisor — thin Python forwarder over the C++ governance core.

The readiness DECISION (capability + index preflight -> READY/UNREADY, fail-closed
EX_CONFIG exit, READY write-gate) is owned by `_core.RuntimeSupervisor` (C++,
`starling::governance`). This module only constructs the supervisor, forwards the
`runtime.health_changed` notification (the C++ supervisor has no event log in
Phase 1 — that is Phase 2 host-glue), and exposes the facade shape (`Runtime`,
the bus stubs). No required-capability list, preflight algorithm, or raw index
read remains in Python after P3.c1 Phase 1.

The behavior contract enforced here (PRECONDITION_FAILED on UNREADY, no worker
start, exit code 78) is what TC-NEW-PREFLIGHT [CRITICAL] locks down for P1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from starling import _core

EX_CONFIG = _core.kExConfig  # single source of truth: C++ governance::kExConfig (78)


def required_capabilities(embedded: bool = False) -> tuple[str, ...]:
    """Thin forwarder to the C++ pure capability policy (no global mutation)."""
    return tuple(_core.required_capabilities(embedded))


class RuntimeUnreadyError(RuntimeError):
    def __init__(self, missing_capabilities: list[str]):
        # Store the list as args[0] so copy / cross-process round-trips
        # reconstruct the error correctly via the default __reduce__ path.
        # __str__ formats lazily so str(e) stays human-readable.
        super().__init__(list(missing_capabilities))
        self.missing_capabilities = list(missing_capabilities)

    def __str__(self) -> str:
        return "preflight failed: " + ", ".join(self.missing_capabilities)


@dataclass
class _StubBus:
    check_write: Callable[[], Any]  # () -> _core.WriteGateDecision
    written_count: int = 0

    def append_evidence(self, _engram) -> str:
        if self.check_write() != _core.WriteGateDecision.kAccept:
            return "PRECONDITION_FAILED"
        self.written_count += 1
        return "OK"

    def write(self, _stmt) -> str:
        if self.check_write() != _core.WriteGateDecision.kAccept:
            return "PRECONDITION_FAILED"
        self.written_count += 1
        return "OK"


@dataclass
class _StubEngramStore:
    appended_count: int = 0


@dataclass
class Runtime:
    capability: _core.ProfileCapability
    on_health_change: Optional[Callable[[dict], None]] = None
    idx_statement_id_tenant_present: Callable[[], bool] = field(default=lambda: True)
    adapter: Optional[Any] = None
    embedded: bool = False

    foreground_workers_started: bool = False
    background_workers_started: bool = False
    exit_code: Optional[int] = None
    bus: Any = field(init=False)
    engram_store: _StubEngramStore = field(default_factory=_StubEngramStore)
    _sup: Any = field(init=False, default=None)

    def __post_init__(self):
        if self.adapter is None:
            # Test-seam path: the index probe is the injected Python callable.
            self._sup = _core.RuntimeSupervisor(
                self.capability, self.embedded, self.idx_statement_id_tenant_present)
            self.bus = _StubBus(check_write=self._sup.check_write)
        else:
            # Production path: the index probe is C++ (adapter.has_index), bound
            # via the SqliteAdapter& ctor — no governance decision through Python.
            self._sup = _core.RuntimeSupervisor(
                self.capability, self.embedded, self.adapter)
            # 前台写门(P3.c):把 adapter 钩子接到 supervisor 健康态。仅 production
            # (adapter 提供)接线;test-seam(adapter=None)与 bare-adapter 测试无钩子 → 放行。
            _core.install_write_gate(self.adapter, self._sup)
            # M0.3 bus surface: the real C++ Bus via BusFacade. Lazy-import to avoid a
            # circular dependency (starling.__init__ → runtime → append_evidence → _core).
            # (前台写门已由 install_write_gate 在 C++ 核心接管;旧的 Python _SqliteBackedBus
            #  门是 PR #45 后的死代码,已删。)
            from starling.bus.append_evidence import BusFacade
            self.bus = BusFacade(self.adapter)

    def health(self) -> _core.RuntimeHealth:
        return self._sup.health()

    def events(self) -> list:
        """Snapshot of the supervisor's transition event log (forwards to C++)."""
        return self._sup.events()

    def last_event(self):
        """Latest transition event, or None (forwards to C++)."""
        return self._sup.last_event()

    def begin_drain(self, trigger: str = "admin_drain") -> None:
        """Enter DRAINING (host shutdown). Forwards to the C++ supervisor; the
        supervisor self-locks, so no Python-side lock is taken here."""
        self._sup.begin_drain(trigger)

    def note_health(self, decision) -> None:
        """Apply a HealthDecision to the live C++ supervisor (Phase 5 backpressure
        sampler path). The supervisor self-locks; no Python lock is taken here.
        Lock order: engine-RLock → supervisor-mutex (L8); NEVER take the
        supervisor mutex first while holding the engine lock in the reverse order."""
        self._sup.note_health(decision)

    def start(self) -> None:
        outcome = self._sup.start()
        # D-P2-2: the C++ supervisor is the event source. start() recorded exactly
        # one transition (-> UNREADY or -> READY); the forwarder maps THAT event to
        # the legacy runtime.health_changed dict — no Python-side state/missing
        # computation. (OV-6: last_event(), not events()[-1].)
        evt = self._sup.last_event()
        if outcome == _core.StartOutcome.kUnready:
            self.foreground_workers_started = False
            self.background_workers_started = False
            self.exit_code = EX_CONFIG
            missing = list(evt.missing_capabilities) if evt is not None else []
            self._emit_health(evt)
            raise RuntimeUnreadyError(missing)
        self.foreground_workers_started = True
        self.background_workers_started = True
        self._emit_health(evt)

    def _emit_health(self, evt) -> None:
        """Map a C++ RuntimeHealthEvent to the legacy runtime.health_changed dict
        (D-P2-2). `state` is the C++ enum's own name (single source of truth — the
        enum is bound .value("READY", ...) etc., so .name is exactly "READY"/"UNREADY").
        """
        if self.on_health_change is None or evt is None:
            return
        self.on_health_change({
            "event": "runtime.health_changed",
            "state": evt.current_status.name,
            "missing_capabilities": list(evt.missing_capabilities),
        })


def _build_local_store_sqlite_runtime(db_path: Path) -> "Runtime":
    """Construct a Runtime backed by a real SqliteAdapter at db_path.

    The embedded single-process facade runs the reduced capability set
    (testing_helper_marker test-only; engram_per_record_key deferred to
    M0.4+KMS), so the supervisor is built with embedded=True. The index probe is
    the bound C++ SqliteAdapter::has_index (no raw Python sqlite3 read).
    """
    return _runtime_over(_core.SqliteAdapter.open(str(db_path)))


def _build_read_only_sqlite_runtime(db_path: Path) -> "Runtime":
    """同 _build_local_store_sqlite_runtime，但库经 SqliteAdapter.open_read_only 打开（spec §6.3）：不跑迁移、
    不改日志模式、不建目录；write_admitted() 恒为 False，经写门的写入抛 WriteGateRejected。"""
    return _runtime_over(_core.SqliteAdapter.open_read_only(str(db_path)))


def _runtime_over(adapter) -> "Runtime":
    cap = adapter.declare_capability()
    rt = Runtime(
        capability=cap,
        adapter=adapter,
        embedded=True,
        idx_statement_id_tenant_present=lambda: adapter.has_index("idx_statement_id_tenant"),
    )
    # rt.bus is BusFacade — set in Runtime.__post_init__ (adapter branch).
    return rt


__all__ = [
    "Runtime",
    "RuntimeUnreadyError",
    "EX_CONFIG",
    "_build_local_store_sqlite_runtime",
    "_build_read_only_sqlite_runtime",
]
