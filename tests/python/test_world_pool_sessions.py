"""引擎池与客户端会话表（记忆服务第 4 期 Task 4.3，spec §6.4、§10.3、§11；裁定 R30、R37、R41、R49、R53、R59–R61、R66–R68）。

池拿着 C++ 的 SessionTable 与一个注入的单调时钟（毫秒）。判定都在 C++（world_decide_open_mode、world_read_only_frozen、
world_write_held 等），这里钉住的是池的编排：冻结与打开方式带上写占用，可回收世界不起后台任务，list_sessions 在世界锁内调 C++。
"""
import gc
import threading
import time

import pytest

from starling import _core
from starling.dashboard.config import DashboardConfig
from starling.dashboard.world_manager import WorldPool


class _Clock:
    """注入的单调时钟（毫秒）：测试手动推进。"""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


def _cfg(tmp_path, **over):
    base = dict(db_path=str(tmp_path / "dashboard.db"), token="", tick_interval_s=600.0,
                source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"))
    base.update(over)
    return DashboardConfig(**base)


@pytest.fixture
def setup(tmp_path):
    cfg = _cfg(tmp_path)
    reg = _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)
    clock = _Clock()
    sessions = _core.SessionTable()
    pool = WorldPool(reg, cfg, monotonic_ms=clock)
    pool.attach_sessions(sessions)
    pool.sync_default_settings()
    pool.create_world("src", "")
    pool.fork_world("src", "rollout-1", reclaimable=True)
    try:
        yield pool, sessions, clock
    finally:
        pool.close_all()
        gc.collect()


def _engine_of(pool, world):
    with pool.lease(world, build=False) as engine:
        return engine


def _writer(sessions, clock, world="rollout-1"):
    """直接在会话表上登记一个可写句柄（admit_client_handle 在单元 3；这里只看判定怎么跟着写占用走）。"""
    sid = sessions.connect(clock())
    return sid, sessions.open_handle(sid, world, "alice", False, clock())


# ───────────────────────── 注入 ─────────────────────────

def test_pool_without_sessions_treats_every_world_as_not_held(tmp_path):
    """没有会话表的旧调用方（测试里直接建的池）按 held=False：可回收世界照旧只读、冻结。"""
    cfg = _cfg(tmp_path)
    reg = _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)
    pool = WorldPool(reg, cfg)
    try:
        pool.sync_default_settings()
        pool.create_world("src", "")
        pool.fork_world("src", "rollout-1", reclaimable=True)
        assert pool.sessions is None
        ctx = pool.context("rollout-1")
        assert ctx.read_only is True and ctx.frozen is True
        assert isinstance(pool.now_ms(), int)
    finally:
        pool.close_all()


def test_now_ms_is_the_injected_clock(setup):
    pool, sessions, clock = setup
    assert pool.sessions is sessions
    clock.now = 42_000
    assert pool.now_ms() == 42_000


# ───────────────────────── R41 / R49 (c)：冻结判定带上写占用 ─────────────────────────

def test_freeze_follows_the_write_hold_including_in_flight_calls(setup):
    pool, sessions, clock = setup
    assert pool.context("rollout-1").frozen is True and pool._frozen("rollout-1") is True
    _sid, handle = _writer(sessions, clock)
    assert pool.context("rollout-1").frozen is False
    assert pool._frozen("rollout-1") is False
    sessions.call_enter(handle, clock())
    sessions.close_handle(handle)                  # 关了，但写调用还在途（裁定 R59）：照样占着
    assert pool.context("rollout-1").frozen is False
    sessions.call_exit(handle, clock())
    assert pool.context("rollout-1").frozen is True
    assert pool._frozen("rollout-1") is True


def test_freeze_uses_the_injected_clock(setup):
    """会话租约按池的时钟过期：时钟推过租约（30 秒）且没有在途调用，写占用随即不算，冻结回到 True。"""
    pool, sessions, clock = setup
    _writer(sessions, clock)
    assert pool.context("rollout-1").frozen is False
    clock.now += 30_000
    assert pool.context("rollout-1").frozen is True


# ───────────────────────── R67：打开方式带上写占用 ─────────────────────────

def test_held_reclaimable_world_rebuilds_as_a_writable_engine_after_close(setup):
    pool, sessions, clock = setup
    with pool.lease("rollout-1"):
        pass
    assert _engine_of(pool, "rollout-1").read_only is True        # 没人占着：看板只读打开
    _sid, handle = _writer(sessions, clock)
    pool.close_world("rollout-1")                                  # 有活句柄照旧允许关（spec §10.3）
    with pool.lease("rollout-1") as engine:
        assert engine.read_only is False                           # 占着写入：重建成可写引擎
        assert engine._rt.adapter.read_only is False
        engine = None
    sessions.close_handle(handle)
    pool.close_world("rollout-1")
    with pool.lease("rollout-1") as engine:
        assert engine.read_only is True                            # 没人占着了：又只读
        engine = None


# ───────────────────────── R68：可回收世界不跑后台任务 ─────────────────────────

def test_reclaimable_world_opened_writable_runs_no_background(setup):
    pool, sessions, clock = setup
    assert pool.record("rollout-1")["settings"]["background"]["ingest"] is True   # 存着的设置是开着的
    pool.start_background()
    _writer(sessions, clock)
    with pool.lease("rollout-1") as engine:
        assert engine.read_only is False
        assert engine._tick_thread is None and engine._ingest_thread is None
        assert engine.session_snapshot_hook is None
        assert engine._settings["background"]["ingest"] is False
        assert engine._settings["background"]["tick_interval_s"] == 0.0
        engine = None
    with pool.lease("src") as engine:                              # 对照：普通世界照常起
        assert engine._tick_thread is not None and engine._ingest_thread is not None
        assert engine.session_snapshot_hook is not None
        engine = None


def test_update_settings_on_a_reclaimable_world_keeps_background_off(setup):
    """PUT 设置换给开着的引擎的同样是生效的设置（裁定 R68）：存着的 background 改了，可回收世界的引擎照旧全关。"""
    pool, sessions, clock = setup
    _writer(sessions, clock)
    with pool.lease("rollout-1"):
        pass
    settings, _warnings = pool.update_settings("rollout-1", {"background": {"tick_interval_s": 5.0}})
    assert pool.record("rollout-1")["settings"]["background"]["tick_interval_s"] == 5.0
    assert settings["background"]["tick_interval_s"] == 0.0
    assert _engine_of(pool, "rollout-1")._settings["background"]["tick_interval_s"] == 0.0


# ───────────────────────── R66：list_sessions 在世界锁内调 C++ ─────────────────────────

def test_list_sessions_waits_for_the_world_lock(setup):
    pool, _sessions, _clock = setup
    lock = pool._world_lock("rollout-1")
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    out: dict = {}

    def holder():
        with lock:
            held.set()
            release.wait(10)

    def lister():
        out["listed"] = pool.list_sessions("rollout-1")
        done.set()

    blocker = threading.Thread(target=holder)
    blocker.start()
    assert held.wait(5)
    worker = threading.Thread(target=lister)
    worker.start()
    assert not done.wait(0.3), "list_sessions must wait for the world lock"
    release.set()
    blocker.join(5)
    assert done.wait(10)
    worker.join(5)
    assert out["listed"]["total"] == 0


def test_list_sessions_unknown_world_does_not_create_a_world_lock(setup):
    pool, _sessions, _clock = setup
    from starling.dashboard.world_manager import WorldSwitchError
    with pytest.raises(WorldSwitchError) as ei:
        pool.list_sessions("nope")
    assert ei.value.code == "world_not_found"
    assert "nope" not in pool._world_locks


# ───────────────────────── 看板：写占用期间检视连接不冻结（R41，context 第 8 条第 2 项） ─────────────────────────

def test_app_lifespan_attaches_one_session_table_and_inspect_is_shared_while_held(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from starling.dashboard import create_app
    from starling.dashboard import queries

    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state
        pool, sessions = state.pool, state.sessions
        assert isinstance(sessions, _core.SessionTable) and pool.sessions is sessions
        pool.create_world("src", "")
        pool.fork_world("src", "rollout-1", reclaimable=True)
        seen: list = []
        real_open_ro = queries.open_ro

        def _spy(db_path, frozen=False):
            seen.append(frozen)
            return real_open_ro(db_path, frozen=frozen)
        monkeypatch.setattr(queries, "open_ro", _spy)

        assert client.get("/api/w/rollout-1/statements").status_code == 200
        sid = sessions.connect(pool.now_ms())
        handle = sessions.open_handle(sid, "rollout-1", "alice", False, pool.now_ms())
        sessions.call_enter(handle, pool.now_ms())
        assert client.get("/api/w/rollout-1/statements").status_code == 200
        sessions.close_handle(handle)                       # 关了、写调用还在途：照样不冻结
        assert client.get("/api/w/rollout-1/statements").status_code == 200
        sessions.call_exit(handle, pool.now_ms())
        assert client.get("/api/w/rollout-1/statements").status_code == 200
        assert seen == [True, False, False, True]


# ═════════════════════════ 单元 3：admit_client_handle 与只读→可写切换（R30、R37、R53、R61） ═════════════════════════

import weakref  # noqa: E402 — 单元 3 的用例才用到

from starling.dashboard.world_manager import WorldSwitchError, _ClosingWindow  # noqa: E402


def _admit(pool, sessions, world="rollout-1", *, read_only=False, sid=None, holder="alice"):
    sid = sid or sessions.connect(pool.now_ms())
    return sid, pool.admit_client_handle(world, sid, holder, read_only, pool.now_ms())


def _code(excinfo) -> str:
    exc = excinfo.value
    return exc.code if isinstance(exc, WorldSwitchError) else _core.world_store_error_code(exc)


def test_admit_writable_swaps_the_read_only_engine_and_reclaims_its_runtime(setup):
    """context 第 8 条第 1 项（裁定 R30）：看板先只读打开可回收世界，客户端可写打开后池里换成可写引擎，旧 Runtime 已回收。"""
    pool, sessions, _clock = setup
    with pool.lease("rollout-1") as engine:
        assert engine.read_only is True
        old_rt = weakref.ref(engine._rt)
        engine = None
    gc.collect()
    assert old_rt() is not None                        # 还在池里
    _sid, handle = _admit(pool, sessions)
    assert sessions.resolve(handle, pool.now_ms()).read_only is False
    assert old_rt() is None, "the read-only engine's Runtime must be gone before the writer"
    engine = _engine_of(pool, "rollout-1")
    assert engine.read_only is False and engine._rt.adapter.read_only is False
    engine = None
    assert sessions.world_write_held("rollout-1", pool.now_ms())


def test_admit_writable_when_no_engine_builds_a_writable_engine(setup):
    pool, sessions, _clock = setup
    assert "rollout-1" not in pool.open_worlds()
    _admit(pool, sessions)
    assert _engine_of(pool, "rollout-1").read_only is False


def test_inspect_through_the_app_after_admit_is_shared_then_frozen(setup, monkeypatch):
    """context 第 8 条第 2 项经 admit 走一遍：写占用期间 build=False 检视的 frozen 为 False；关句柄、在途调用归零后回到 True。"""
    pool, sessions, _clock = setup
    from starling.dashboard import queries
    seen: list = []
    real_open_ro = queries.open_ro

    def _inspect():
        with pool.lease("rollout-1", build=False):
            ctx = pool.context("rollout-1")
            with real_open_ro(ctx.db_path, frozen=ctx.frozen) as conn:
                conn.execute("SELECT 1").fetchall()
            seen.append(ctx.frozen)
    _inspect()
    _sid, handle = _admit(pool, sessions)
    sessions.call_enter(handle, pool.now_ms())
    _inspect()
    sessions.call_exit(handle, pool.now_ms())
    pool.close_client_handle(handle, timeout_s=1.0)
    pool.close_world("rollout-1")                      # 写者走了：-wal/-shm 随之合并删除
    _inspect()
    assert seen == [True, False, True]


def test_admit_writable_waits_for_an_inspect_lease(setup):
    """context 第 8 条第 3 项（裁定 R37、R53）：检视请求持着 build=False 租约时，可写 admit 等它结束，不与它并行。"""
    pool, sessions, _clock = setup
    order: list[str] = []
    holding, release = threading.Event(), threading.Event()
    admitted = threading.Event()

    def inspect():
        with pool.lease("rollout-1", build=False):
            holding.set()
            release.wait(10)
            order.append("inspect-done")

    def admit():
        _admit(pool, sessions)
        order.append("admitted")
        admitted.set()

    reader = threading.Thread(target=inspect)
    reader.start()
    assert holding.wait(5)
    writer = threading.Thread(target=admit)
    writer.start()
    assert not admitted.wait(0.4), "admit must wait for the inspect lease to drain"
    assert not sessions.world_write_held("rollout-1", pool.now_ms())   # 等的时候还没登记写占用
    release.set()
    reader.join(5)
    assert admitted.wait(10)
    writer.join(5)
    assert order == ["inspect-done", "admitted"]


def test_admit_while_the_world_is_in_a_closing_window_is_world_busy(setup):
    """context 第 8 条第 6 项：世界处在关闭窗口里（关闭、恢复、删除）时 admit 回 world_busy，可写、只读都一样，什么都不登记。"""
    pool, sessions, _clock = setup
    inside, release = threading.Event(), threading.Event()

    def closer():
        with pool._world_lock("rollout-1"), _ClosingWindow(pool, "rollout-1"):
            inside.set()
            release.wait(10)

    worker = threading.Thread(target=closer)
    worker.start()
    try:
        assert inside.wait(5)
        for read_only in (False, True):
            started = time.monotonic()
            with pytest.raises(WorldSwitchError) as ei:
                _admit(pool, sessions, read_only=read_only)
            assert ei.value.code == "world_busy"
            assert time.monotonic() - started < 2.0          # 不等关闭窗口结束
        assert not sessions.world_has_live_handles("rollout-1", pool.now_ms())
    finally:
        release.set()
        worker.join(5)


def test_admit_gives_up_when_a_closing_window_opens_while_it_waits(setup):
    """admit 在等世界锁（快照之类的短操作持着它）时，世界进了关闭窗口：放弃，world_busy。"""
    pool, sessions, _clock = setup
    holding, open_window, release = threading.Event(), threading.Event(), threading.Event()
    result: dict = {}

    def snapshotter():
        with pool._world_lock("rollout-1"):
            holding.set()
            open_window.wait(10)
            with _ClosingWindow(pool, "rollout-1"):
                release.wait(10)

    def admit():
        try:
            _admit(pool, sessions)
        except WorldSwitchError as exc:
            result["code"] = exc.code

    worker = threading.Thread(target=snapshotter)
    worker.start()
    assert holding.wait(5)
    admitter = threading.Thread(target=admit)
    admitter.start()
    time.sleep(0.2)
    open_window.set()
    admitter.join(5)
    release.set()
    worker.join(5)
    assert result.get("code") == "world_busy"


@pytest.mark.parametrize("kind", ["forked_seed", "empty_seed"])
def test_admit_writable_on_a_seed_is_world_read_only(setup, kind):
    """种子（蕴含 read_only，registry_db.cpp 的 seed ⇒ read_only）：分叉出来的与注册表直接建的空种子都拒绝可写。"""
    pool, sessions, _clock = setup
    if kind == "forked_seed":
        pool.fork_world("src", "ro-1", seed=True)
    else:
        pool.registry.create("ro-1", "", {}, None, "2026-11-07T00:00:00Z", seed=True)
    assert pool.record("ro-1")["read_only"] is True
    with pytest.raises(_core.WorldStoreError) as ei:
        _admit(pool, sessions, "ro-1")
    assert _code(ei) == "world_read_only"
    assert not sessions.world_has_live_handles("ro-1", pool.now_ms())
    _sid, handle = _admit(pool, sessions, "ro-1", read_only=True)        # 只读句柄可以
    assert sessions.resolve(handle, pool.now_ms()).read_only is True


def test_second_session_writable_admit_is_world_held_and_keeps_the_engine(setup):
    pool, sessions, _clock = setup
    _admit(pool, sessions)
    engine = _engine_of(pool, "rollout-1")
    rt = weakref.ref(engine._rt)
    engine = None
    with pytest.raises(_core.WorldStoreError) as ei:
        _admit(pool, sessions)
    assert _code(ei) == "world_held"
    assert rt() is not None and _engine_of(pool, "rollout-1")._rt is rt()   # 可写引擎没被换掉
    _sid, handle = _admit(pool, sessions, read_only=True)                     # 别的会话只读可以
    assert sessions.resolve(handle, pool.now_ms()).read_only is True


def test_admit_writable_with_an_existing_writable_engine_does_not_drain(setup):
    """R61 第 2 条：池里已有可写引擎时不用等（写者已经在了）。持着 build=False 租约也立刻登记。"""
    pool, sessions, _clock = setup
    sid, _first = _admit(pool, sessions)
    engine = _engine_of(pool, "rollout-1")
    with pool.lease("rollout-1", build=False):
        started = time.monotonic()
        _sid, handle = _admit(pool, sessions, sid=sid, holder="bob")         # 同一会话第二个可写句柄
        assert time.monotonic() - started < 1.0
    assert _engine_of(pool, "rollout-1") is engine
    engine = None
    assert sessions.resolve(handle, pool.now_ms()).holder == "bob"


def test_admit_read_only_does_not_switch_and_opens_like_the_dashboard(setup):
    pool, sessions, _clock = setup
    _sid, handle = _admit(pool, sessions, read_only=True)                     # 没有引擎：按看板的方式打开
    assert _engine_of(pool, "rollout-1").read_only is True
    assert not sessions.world_write_held("rollout-1", pool.now_ms())
    with pool.lease("src"):
        pass
    engine = _engine_of(pool, "src")
    _admit(pool, sessions, "src", read_only=True)
    assert _engine_of(pool, "src") is engine                                 # 已有引擎：不动
    engine = None
    assert sessions.resolve(handle, pool.now_ms()).read_only is True


def test_admit_with_an_expired_session_builds_nothing(setup):
    pool, sessions, clock = setup
    sid = sessions.connect(clock())
    clock.now += 30_000
    with pytest.raises(_core.WorldStoreError) as ei:
        pool.admit_client_handle("rollout-1", sid, "alice", False, pool.now_ms())
    assert _code(ei) == "session_expired"
    assert "rollout-1" not in pool.open_worlds()
    assert not pool.closing_worlds()


def test_admit_unknown_world_is_world_not_found(setup):
    pool, sessions, _clock = setup
    with pytest.raises(WorldSwitchError) as ei:
        _admit(pool, sessions, "nope")
    assert ei.value.code == "world_not_found"
    assert "nope" not in pool._world_locks


def test_admit_without_a_session_table_is_worlds_unavailable(tmp_path):
    cfg = _cfg(tmp_path)
    reg = _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)
    pool = WorldPool(reg, cfg)
    try:
        pool.create_world("w1", "")
        with pytest.raises(WorldSwitchError) as ei:
            pool.admit_client_handle("w1", "s", "alice", False, 0)
        assert ei.value.code == "worlds_unavailable"
    finally:
        pool.close_all()


def test_admit_writable_open_failure_releases_the_hold(setup, monkeypatch):
    """可写引擎建不起来（world_open_failed）：写占用撤销，世界不在关闭窗口里，下一次照常可以再试。"""
    pool, sessions, _clock = setup

    def _boom(_ctx, _providers):
        raise RuntimeError("cannot open")
    monkeypatch.setattr(pool, "_factory", _boom)
    with pytest.raises(WorldSwitchError) as ei:
        _admit(pool, sessions)
    assert ei.value.code == "world_open_failed"
    assert not sessions.world_write_held("rollout-1", pool.now_ms())
    assert not pool.closing_worlds() and "rollout-1" not in pool.open_worlds()
    monkeypatch.undo()
    _admit(pool, sessions)
    assert _engine_of(pool, "rollout-1").read_only is False


def test_close_client_handle_waits_for_in_flight_calls_and_times_out_open(setup):
    """close(timeout)：等这个句柄的在途调用归零；超时 world_busy、句柄保持打开。"""
    pool, sessions, _clock = setup
    _sid, handle = _admit(pool, sessions)
    sessions.call_enter(handle, pool.now_ms())
    started = time.monotonic()
    with pytest.raises(WorldSwitchError) as ei:
        pool.close_client_handle(handle, timeout_s=0.2)
    assert ei.value.code == "world_busy"
    assert 0.15 <= time.monotonic() - started < 2.0
    assert sessions.resolve(handle, pool.now_ms()).world == "rollout-1"      # 还开着

    def _exit_later():
        time.sleep(0.2)
        sessions.call_exit(handle, pool.now_ms())
    threading.Thread(target=_exit_later).start()
    pool.close_client_handle(handle, timeout_s=5.0)
    with pytest.raises(_core.WorldStoreError) as ei:
        sessions.resolve(handle, pool.now_ms())
    assert _code(ei) == "handle_closed"
    assert not sessions.world_write_held("rollout-1", pool.now_ms())
    pool.close_client_handle(handle, timeout_s=0.0)                          # 幂等
