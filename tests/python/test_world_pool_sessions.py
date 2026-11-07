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
