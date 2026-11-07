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
    """PUT 设置换给开着的引擎的同样是生效的设置（裁定 R68）：存着的 background 改了，可回收世界的引擎照旧全关；之后被客户端
    占着写入、重建成可写引擎，按改过的设置也照旧全关。（被占着时 PUT 设置本身回 world_held，裁定 R73，见单元 4。）"""
    pool, sessions, clock = setup
    with pool.lease("rollout-1"):
        pass
    settings, _warnings = pool.update_settings("rollout-1", {"background": {"tick_interval_s": 5.0}})
    assert pool.record("rollout-1")["settings"]["background"]["tick_interval_s"] == 5.0
    assert settings["background"]["tick_interval_s"] == 0.0
    assert _engine_of(pool, "rollout-1")._settings["background"]["tick_interval_s"] == 0.0
    _writer(sessions, clock)
    pool.close_world("rollout-1")
    with pool.lease("rollout-1") as engine:
        assert engine.read_only is False
        assert engine._settings["background"]["tick_interval_s"] == 0.0
        engine = None


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


# ═════════════════════════ 单元 4：看板写入准入、恢复与删除检查句柄（R25、R51、R60、R61、R73） ═════════════════════════

import hashlib  # noqa: E402
import os  # noqa: E402


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


_STUB_JSON = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
              '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
              '"polarity":"POS","nesting_depth":0}]')


def _stub_factory(ctx, providers):
    from starling.dashboard.engine import DashboardEngine
    engine = DashboardEngine(ctx, providers)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB_JSON, True, "")
    engine.llm = fake
    return engine


@pytest.fixture
def app_client(tmp_path):
    from fastapi.testclient import TestClient

    from starling.dashboard import create_app
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg, engine_factory=_stub_factory)) as client:
        pool = client.app.state.pool
        pool.create_world("w1", "")
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        pool.fork_world("w1", "rollout-1", reclaimable=True)
        yield client


def _held_error(code: str) -> dict:
    row = {r["code"]: r for r in _core.error_catalog()}[code]
    return {"code": code, "transient": bool(row["transient"]), "detail": code}


def _content_writes(stmt_id: str):
    return (("/remember", {"text": "Carol owns billing"}),
            ("/converse", {"message": "who owns billing?"}),
            ("/tick", {}),
            ("/forget", {"ids": [stmt_id]}),
            ("/clear_memory", {"confirm": "w1"}),
            ("/review", {"stmt_id": stmt_id}),
            ("/replay_trigger", {"mode": "idle"}),
            ("/reconsolidate", {"stmt_id": stmt_id}),
            ("/commitment/fulfill", {"stmt_id": stmt_id}),
            ("/commitment/withdraw", {"stmt_id": stmt_id}))


@pytest.mark.parametrize("world", ["w1", "rollout-1"])
def test_dashboard_writes_on_a_client_held_world_are_409_world_held(app_client, world):
    """context 第 8 条第 4 项（裁定 R51）：被客户端占着写入的普通世界与可回收世界，看板写入一律 409 world_held（不是
    world_read_only）；召回、检视照常。库文件不变。"""
    client = app_client
    pool, sessions = client.app.state.pool, client.app.state.sessions
    stmt_id = client.get(f"/api/w/{world}/statements").json()["rows"][0]["id"]
    sid = sessions.connect(pool.now_ms())
    pool.admit_client_handle(world, sid, "alice", False, pool.now_ms())
    db = pool.record(world)["db_path"]
    pool_engine = _engine_of(pool, world)
    rows_before = client.get(f"/api/w/{world}/statements").json()["rows"]
    pool_engine = None
    for path, body in _content_writes(stmt_id):
        resp = client.post(f"/api/w/{world}{path}", json=body)
        assert (resp.status_code, resp.json()) == (409, _held_error("world_held")), path
    with client.websocket_connect("/ws/converse") as ws:
        ws.send_json({"message": "who owns billing?", "world": world})
        assert ws.receive_json() == {"type": "error", "error": "world_held"}
    # R73：三条设置路由被占用时同样 world_held。
    for method, path, body in (("put", f"/api/worlds/{world}/settings", {"gist_thresholds": {"min_holders": 4}}),
                               ("post", f"/api/worlds/{world}/settings/reconfirm", None),
                               ("put", f"/api/worlds/{world}/auto_snapshot",
                                {"on_session": True, "interval_hours": 6})):
        resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert (resp.status_code, resp.json()) == (409, _held_error("world_held")), path
    resp = client.post(f"/api/w/{world}/recall", json={"query": "who owns auth"})
    assert resp.status_code == 200, resp.text                                   # 召回照常
    assert client.get(f"/api/w/{world}/statements").json()["rows"] == rows_before
    assert client.get(f"/api/w/{world}/working_set", params={"interlocutor": "claude"}).status_code == 200
    assert os.path.exists(db)


def test_settings_routes_on_a_free_reclaimable_world_keep_r38(app_client):
    """R73：设置路由只看写占用，不判只读（它们写的是 registry.db）。没人占着的可回收世界照旧 200（R38）。"""
    client = app_client
    resp = client.put("/api/worlds/rollout-1/auto_snapshot", json={"on_session": True, "interval_hours": 6})
    assert resp.status_code == 200, resp.text
    resp = client.put("/api/worlds/rollout-1/settings", json={"gist_thresholds": {"min_holders": 4}})
    assert resp.status_code == 200, resp.text
    # 看板内容写入照旧 world_read_only（没人占着：可回收世界看板只读）。
    resp = client.post("/api/w/rollout-1/remember", json={"text": "x"})
    assert (resp.status_code, resp.json()) == (409, _held_error("world_read_only"))


def test_dashboard_write_after_the_hold_is_released_succeeds(app_client):
    client = app_client
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = sessions.connect(pool.now_ms())
    handle = pool.admit_client_handle("w1", sid, "alice", False, pool.now_ms())
    assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 409
    pool.close_client_handle(handle, timeout_s=1.0)
    assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
    sid2 = sessions.connect(pool.now_ms())
    pool.admit_client_handle("w1", sid2, "bob", True, pool.now_ms())          # 只读句柄不占写入
    assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200


def test_rollback_with_a_live_handle_is_world_held_and_leaves_the_file(app_client):
    """context 第 8 条第 5 项（裁定 R25、R61 (3)）：有活句柄（只读也算）时恢复回 world_held，库文件、归档都不动。"""
    client = app_client
    pool, sessions = client.app.state.pool, client.app.state.sessions
    snap = client.post("/api/worlds/w1/snapshots", json={}).json()
    assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
    for read_only in (True, False):
        sid = sessions.connect(pool.now_ms())
        handle = pool.admit_client_handle("w1", sid, "alice", read_only, pool.now_ms())
        pool.close_world("w1")                          # 合并 WAL，sha256 才稳定
        db = pool.record("w1")["db_path"]
        sha = _sha256(db)
        resp = client.post("/api/worlds/w1/rollback",
                           json={"snapshot_id": snap["id"], "confirm": "w1"})
        assert (resp.status_code, resp.json()) == (409, _held_error("world_held")), read_only
        assert _sha256(db) == sha
        assert pool.archives("w1") == []
        pool.close_client_handle(handle, timeout_s=1.0)
    resp = client.post("/api/worlds/w1/rollback", json={"snapshot_id": snap["id"], "confirm": "w1"})
    assert resp.status_code == 200, resp.text                                     # 没有句柄了：照常恢复


def test_delete_with_a_live_handle_or_in_flight_call_is_world_in_use_and_deletes_nothing(app_client):
    """context 第 8 条第 5 项（裁定 R60）：删除先问 forget_world，有活句柄或在途调用时 world_in_use，注册项、目录、库都在。"""
    client = app_client
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = sessions.connect(pool.now_ms())
    handle = pool.admit_client_handle("w1", sid, "alice", False, pool.now_ms())
    pool.close_world("w1")
    db = pool.record("w1")["db_path"]
    sha = _sha256(db)
    resp = client.delete("/api/worlds/w1", params={"confirm": "w1"})
    assert (resp.status_code, resp.json()) == (409, _held_error("world_in_use"))
    sessions.call_enter(handle, pool.now_ms())
    sessions.close_handle(handle)                      # 句柄关了，调用还在途
    resp = client.delete("/api/worlds/w1", params={"confirm": "w1"})
    assert (resp.status_code, resp.json()) == (409, _held_error("world_in_use"))
    assert pool.record("w1")["db_path"] == db and _sha256(db) == sha
    sessions.call_exit(handle, pool.now_ms())
    resp = client.delete("/api/worlds/w1", params={"confirm": "w1"})
    assert resp.status_code == 200, resp.text
    assert not os.path.exists(db)
    with pytest.raises(WorldSwitchError):
        pool.record("w1")


def test_delete_forgets_the_world_so_a_recreated_world_starts_fresh(app_client):
    """forget_world 通过之后才删（R60）：删掉的世界的最后活动与墓碑清掉，同名世界重建后从「没有记录」开始（不判可关）。"""
    client = app_client
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sessions.touch_world("w1", pool.now_ms())
    assert client.delete("/api/worlds/w1", params={"confirm": "w1"}).status_code == 200
    assert sessions.may_close_world("w1", False, pool.now_ms() + 10 ** 9) is False


# ═════════════════════════ 单元 5：空闲关闭（R50、R63） ═════════════════════════


def _idle_pool(tmp_path, idle_close_ms=1_000):
    cfg = _cfg(tmp_path, tick_interval_s=0.0)
    reg = _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)
    clock = _Clock()
    sessions = _core.SessionTable(idle_close_ms=idle_close_ms)
    pool = WorldPool(reg, cfg, monotonic_ms=clock)
    pool.attach_sessions(sessions)
    pool.sync_default_settings()
    pool.create_world("w1", "")
    return pool, sessions, clock


def test_lease_and_engine_open_touch_the_world(tmp_path):
    """R50：池打开引擎时与每次 lease（build 与否）都调 touch_world，看板查看算作活动。"""
    pool, sessions, clock = _idle_pool(tmp_path)
    try:
        assert sessions.may_close_world("w1", False, clock() + 10 ** 6) is False     # 没有记录：不判可关
        with pool.lease("w1", build=False):
            pass
        clock.now += 1_001
        assert sessions.may_close_world("w1", False, clock()) is True                # build=False 的租约也记了活动
        with pool.lease("w1"):                                                       # 建引擎 + 租约
            pass
        assert sessions.may_close_world("w1", False, clock()) is False
        clock.now += 1_001
        assert sessions.may_close_world("w1", False, clock()) is True
    finally:
        pool.close_all()


def test_idle_round_closes_only_worlds_the_cpp_judgement_allows(tmp_path):
    """R50、R63：一轮空闲关闭对每个开着的世界问 may_close_world（不是 world_has_live_handles），说可以就 close_world。
    默认世界、有活句柄的、有在途调用的（句柄已关）、时限内有活动的都不关。"""
    pool, sessions, clock = _idle_pool(tmp_path)
    try:
        for name in ("w2", "w3", "w4"):
            pool.create_world(name, "")
        for name in ("default", "w1", "w2", "w3", "w4"):
            with pool.lease(name):
                pass
        sid = sessions.connect(clock())
        live = pool.admit_client_handle("w2", sid, "alice", True, clock())          # w2：活句柄
        busy = pool.admit_client_handle("w3", sid, "bob", False, clock())           # w3：句柄关了、调用在途
        sessions.call_enter(busy, clock())
        sessions.close_handle(busy)
        clock.now += 1_001
        sessions.heartbeat(sid, clock())
        with pool.lease("w4", build=False):                                        # w4：刚有看板活动
            pass
        closed = pool.idle_close_round()
        assert closed == ["w1"]
        assert sorted(pool.open_worlds()) == ["default", "w2", "w3", "w4"]
        sessions.call_exit(busy, clock())
        pool.close_client_handle(live, timeout_s=1.0)
        clock.now += 1_001
        assert sorted(pool.idle_close_round()) == ["w2", "w3", "w4"]
        assert pool.open_worlds() == ["default"]                                   # 默认世界不因空闲关闭
    finally:
        pool.close_all()


def test_idle_round_skips_a_world_that_is_busy(tmp_path):
    """一个世界关不掉（租约排干超时 → world_busy）只记日志，不挡别的世界，下一轮再试。"""
    pool, sessions, clock = _idle_pool(tmp_path)
    try:
        pool.create_world("w2", "")
        for name in ("w1", "w2"):
            with pool.lease(name):
                pass
        clock.now += 1_001
        held = pool.lease("w1", build=False)          # 持着一份租约不还：关闭等不到归零
        try:
            closed = pool.idle_close_round(drain_timeout_s=0.1)
        finally:
            held.release()
        assert closed == ["w2"]
        assert "w1" in pool.open_worlds()
        clock.now += 1_001
        assert pool.idle_close_round() == ["w1"]
    finally:
        pool.close_all()


def test_idle_close_thread_runs_on_the_configured_interval(tmp_path):
    """start_background 起空闲关闭线程，间隔取参数（服务配置项）；stop_background 停它。"""
    pool, sessions, clock = _idle_pool(tmp_path)
    try:
        with pool.lease("w1"):
            pass
        clock.now += 1_001
        pool.start_background(idle_close_interval_s=0.05)
        deadline = time.monotonic() + 5
        while "w1" in pool.open_worlds() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "w1" not in pool.open_worlds()
        assert any(t.name == "starling-idle-close" for t in threading.enumerate())
        pool.stop_background()
        assert not any(t.name == "starling-idle-close" for t in threading.enumerate())
    finally:
        pool.close_all()


def test_idle_close_interval_is_a_service_option(tmp_path):
    """--idle-close-interval（秒）进 create_app → start_background；缺省 30 秒。"""
    from starling.service import serve
    args = serve._parser().parse_args(["--data-dir", str(tmp_path), "--port", "0"])
    assert args.idle_close_interval == 30.0
    args = serve._parser().parse_args(["--data-dir", str(tmp_path), "--port", "0",
                                       "--idle-close-interval", "5"])
    assert args.idle_close_interval == 5.0


# ═════════════════════════ 修复轮：池自己的租约不算活动（R80） ═════════════════════════

def test_pool_internal_leases_do_not_keep_an_idle_world_open(tmp_path, monkeypatch):
    """统计刷新（短间隔跑着）、列表现读计数（live_stats、schema_behind(open_only)）、world_changed 去抖都不 touch：空闲的非默认
    世界到了时限照样被关。看板请求取的租约（缺省 touch）让它继续开着。"""
    pool, sessions, clock = _idle_pool(tmp_path)
    try:
        pool.create_world("w2", "")
        for name in ("w1", "w2"):
            with pool.lease(name):
                pass
        rounds = {"n": 0}
        real_refresh = pool.refresh_stats

        def _counting_refresh():
            real_refresh()
            rounds["n"] += 1
        monkeypatch.setattr(pool, "refresh_stats", _counting_refresh)
        pool.on_world_changed = lambda _world, _stats: None
        pool.start_background(stats_interval_s=0.02, scheduler_first_delay_s=3600.0,
                              idle_close_interval_s=0)
        clock.now += 1_001                                   # 两个世界都过了空闲时限
        seen = rounds["n"]
        deadline = time.monotonic() + 5
        while rounds["n"] < seen + 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rounds["n"] >= seen + 3, "the stats thread must have run while the worlds were idle"
        for name in ("w1", "w2"):
            assert pool.live_stats(name) is not None
            assert pool.schema_behind(name, open_only=True) in (True, False)
            pool._emit_change(name)
        with pool.lease("w2", build=False):                  # 看板请求：算活动
            pass
        assert pool.idle_close_round() == ["w1"]
        assert "w2" in pool.open_worlds()
    finally:
        pool.close_all()
