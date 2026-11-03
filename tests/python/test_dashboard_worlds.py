"""世界注册表与快照（W1/W2）：绑定、切换、路由。

设计：docs/superpowers/specs/2026-10-25-world-scene-management-design.md
"""
import gc
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import weakref

import pytest
from fastapi.testclient import TestClient

from starling import _core
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard import queries
from starling.dashboard.engine import DashboardEngine, SessionSnapshotDeferred
from starling.dashboard.world_manager import (WorldManager, WorldSwitchError,
                                              bootstrap_worlds, registry_path_for)


def _code(exc) -> str:
    return _core.world_store_error_code(exc)


# 三个 test_binding_* 用例（注册表往返、错误码、快照与统计）已迁到 test_world_registry_bindings.py。


# ───────────────────────── W1 Task 4：世界切换（引擎生命周期） ─────────────────────────

_STUB = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
         '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
         '"polarity":"POS","nesting_depth":0}]')


def _cfg(tmp_path):
    return DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                           tick_interval_s=0, source_lane_enabled=False,
                           ingest_spool_path=str(tmp_path / "spool"))


class _State:
    """最小 app.state 替身：WorldManager 只用 .engine 和 .config。"""
    def __init__(self, cfg):
        self.config = cfg
        self.engine = None


def _manager(tmp_path):
    cfg = _cfg(tmp_path)
    reg_path = bootstrap_worlds(cfg)
    state = _State(cfg)
    state.engine = DashboardEngine(cfg)
    return WorldManager(state, reg_path), state


def _remember(engine, text):
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    engine.llm = fake
    return engine.remember(text)


def test_registry_path_defaults_next_to_db(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    assert registry_path_for(_cfg(tmp_path)) == str(tmp_path / "worlds.json")
    monkeypatch.setenv("STARLING_WORLDS_REGISTRY", str(tmp_path / "x" / "r.json"))
    assert registry_path_for(_cfg(tmp_path)) == str(tmp_path / "x" / "r.json")


def test_bootstrap_rewrites_config_to_active_world(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    reg_path = bootstrap_worlds(cfg)
    reg = _core.world_load_registry(reg_path)
    reg = _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    reg = _core.world_set_active(reg, "sim-1")
    _core.world_save_registry(reg_path, reg)
    cfg2 = _cfg(tmp_path)
    bootstrap_worlds(cfg2)
    assert cfg2.tenant == "sim-1"
    assert cfg2.db_path.endswith("worlds/sim-1/world.db")


def test_bootstrap_warns_when_registry_overrides_config(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    bootstrap_worlds(_cfg(tmp_path))                     # 建注册表：default -> dashboard.db
    caplog.clear()
    bootstrap_worlds(_cfg(tmp_path))
    assert "overrides config" not in caplog.text         # 一致时不吵
    other = _cfg(tmp_path)
    other.db_path = str(tmp_path / "other.db")           # 例如 STARLING_DASH_DB 指到别处
    with caplog.at_level("WARNING"):
        bootstrap_worlds(other)
    assert "overrides config" in caplog.text
    assert other.db_path == str(tmp_path / "dashboard.db")


@pytest.mark.parametrize("db_path", [":memory:", "", "bare.db"])
def test_bootstrap_skips_db_without_parent_dir(tmp_path, monkeypatch, db_path):
    """:memory: / 无父目录的路径：不建注册表，不改 config，app.state.worlds 为 None。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = DashboardConfig(db_path=db_path, token="", tick_interval_s=0)
    assert bootstrap_worlds(cfg) is None
    assert (cfg.db_path, cfg.tenant) == (db_path, "default")
    app = create_app(DashboardConfig(db_path=db_path, token="", tick_interval_s=0))
    assert app.state.worlds is None
    assert list(tmp_path.iterdir()) == []          # cwd 里没有悄悄落下 worlds.json


def test_create_app_wires_world_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    assert isinstance(app.state.worlds, WorldManager)
    assert app.state.worlds.registry_path == str(tmp_path / "worlds.json")
    assert app.state.worlds.workers_enabled is False and app.state.worlds.on_tick is None
    assert (tmp_path / "worlds.json").exists()


def test_lifespan_enables_workers_and_switch_restarts_them(tmp_path, monkeypatch):
    """lifespan 起完线程后才打开 workers_enabled 并挂上 on_tick；
    此后切换世界会为新引擎重启 tick/摄入线程，关停时再关掉开关。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    cfg.tick_interval_s = 60.0
    app = create_app(cfg, engine=DashboardEngine(cfg))
    worlds = app.state.worlds
    assert worlds.workers_enabled is False
    with TestClient(app):                              # 进入 lifespan
        assert worlds.workers_enabled is True and worlds.on_tick is not None
        old = app.state.engine
        old_rt = weakref.ref(old._rt)
        old_threads = (old._tick_thread, old._ingest_thread)
        del old                                        # 测试自己不能钉住旧引擎
        worlds.create("sim-1", "")
        worlds.activate("sim-1")
        assert old_rt() is None, "第一次切换就必须释放旧 Runtime"
        new = app.state.engine
        assert all(t is not None and not t.is_alive() for t in old_threads)
        assert new._tick_thread is not None and new._tick_thread.is_alive()
        assert new._ingest_thread is not None and new._ingest_thread.is_alive()
    assert worlds.workers_enabled is False
    assert new._tick_thread is None and new._ingest_thread is None   # 关停时已停


@pytest.mark.parametrize("shape", ["injected", "assigned_after_create"])
@pytest.mark.parametrize("tick", [0, 60.0])
def test_lifespan_first_switch_releases_old_runtime(tmp_path, monkeypatch, caplog, tick, shape):
    """lifespan 在 yield 处挂起期间，它的局部变量一直活着：若其中有 eng 指向启动时的
    引擎，第一次切换就钉住旧 Runtime（第二次才正常释放），默认世界的 C++ 写连接留下，
    切回默认世界就是同一个库文件上的两条写连接。覆盖生产形态（create_app(cfg) 之后
    再给 app.state.engine 赋值）和注入形态，tick 开/关各一遍。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    cfg.tick_interval_s = tick
    if shape == "injected":
        app = create_app(cfg, engine=DashboardEngine(cfg))
    else:
        app = create_app(cfg)
        app.state.engine = DashboardEngine(cfg)
    worlds = app.state.worlds
    with TestClient(app):
        worlds.create("sim-1", "")
        old_rt = weakref.ref(app.state.engine._rt)
        with caplog.at_level("WARNING"):
            worlds.activate("sim-1")
        assert old_rt() is None
        assert "old runtime still referenced" not in caplog.text
        default_rt = weakref.ref(app.state.engine._rt)
        with caplog.at_level("WARNING"):
            worlds.activate("default")                 # 切回默认世界，同一个库文件
        assert default_rt() is None
        assert "old runtime still referenced" not in caplog.text


def test_activate_switches_engine_and_isolates_data(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _remember(state.engine, "Bob owns auth")
    mgr.create("sim-1", "")
    old_rt = weakref.ref(state.engine._rt)
    mgr.activate("sim-1")
    gc.collect()
    assert old_rt() is None, "旧 Runtime 必须被释放（否则同文件会有两条写连接）"
    assert state.config.tenant == "sim-1"
    assert mgr.load()["active"] == "sim-1"
    # 新世界是空的
    con = sqlite3.connect(f"file:{state.config.db_path}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 0
    con.close()
    # 切回默认世界，原数据仍在
    mgr.activate("default")
    con = sqlite3.connect(f"file:{state.config.db_path}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 1
    con.close()


def test_activate_keeps_config_path_and_is_a_copy(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    old_cfg = state.config
    old_cfg.config_path = str(tmp_path / "starling.json")
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    assert state.config is not old_cfg
    assert state.config.config_path == str(tmp_path / "starling.json")
    assert old_cfg.tenant == "default"             # 旧 config 对象不被改动


def test_activate_noop_when_already_active(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    eng, cfg = state.engine, state.config
    mgr.activate("default")
    assert state.engine is eng and state.config is cfg


def test_activate_unknown_world(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("nope")
    assert ei.value.code == "world_not_found"


def test_activate_rolls_back_when_new_world_fails_to_open(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    state.engine.start_background_tick(60.0)
    state.engine.start_ingest_worker()
    _remember(state.engine, "Bob owns auth")
    mgr.create("sim-1", "")
    # 让新世界的库文件是一个目录：SqliteAdapter.open 必然失败
    reg = mgr.load()
    os.makedirs(reg["worlds"]["sim-1"]["db_path"])
    old_cfg = state.config
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "world_open_failed"
    assert state.engine is not None
    assert state.config is old_cfg and state.config.tenant == "default"
    assert mgr.load()["active"] == "default"
    assert mgr.switching is False
    # 回滚回来的引擎真能用：读得到切换前写进去的数据、还能再写、健康、线程在跑
    assert state.engine.health().name == "READY"
    _remember(state.engine, "Carol owns billing")
    con = sqlite3.connect(f"file:{state.config.db_path}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 2
    con.close()
    assert state.engine._tick_thread is not None and state.engine._tick_thread.is_alive()
    assert state.engine._ingest_thread is not None and state.engine._ingest_thread.is_alive()
    assert state.engine.quiesce() is True


def test_activate_rollback_failure_surfaces_as_world_open_failed(tmp_path, monkeypatch):
    """新世界打不开、回滚重建旧世界也失败：仍是 WorldSwitchError（路由只映射它），
    不是裸异常；engine 留 None，config 仍是旧世界，之后惰性重建得到的是旧世界。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    real = wm.DashboardEngine

    def _always_fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _always_fail)
    old_cfg = state.config
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "world_open_failed"
    assert "disk on fire" in ei.value.detail
    assert state.engine is None and state.config is old_cfg
    assert mgr.load()["active"] == "default" and mgr.switching is False
    monkeypatch.setattr(wm, "DashboardEngine", real)
    assert real(state.config).health().name == "READY"      # 旧世界的库并没有被破坏


def test_activate_refuses_when_ingest_will_not_stop(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    eng, cfg = state.engine, state.config
    eng.start_background_tick(60.0)
    eng.start_ingest_worker()
    mgr.create("sim-1", "")

    def _stopped_but_reports_busy(timeout_s=5.0):
        eng.stop_ingest_worker()
        eng.stop_background_tick()
        return False
    monkeypatch.setattr(eng, "quiesce", _stopped_but_reports_busy)
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "ingest_busy"
    assert mgr.load()["active"] == "default"
    assert state.engine is eng and state.config is cfg      # 状态不前进
    assert mgr.switching is False
    # 失败后必须把后台线程恢复出来，否则摄入悄悄停摆
    assert eng._tick_thread is not None and eng._tick_thread.is_alive()
    assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
    monkeypatch.undo()
    assert eng.quiesce() is True


class _StuckThread:
    """join 不动、一直 is_alive 的线程替身。"""
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def test_ingest_busy_detail_names_the_stuck_thread(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    eng = state.engine
    eng._tick_thread, eng._tick_stop = _StuckThread(), threading.Event()   # 卡的是 tick
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "ingest_busy"                   # 前端按这个字符串映射，码不变
    assert "tick" in ei.value.detail and "ingest" not in ei.value.detail
    eng._tick_thread = eng._tick_stop = None
    eng._ingest_thread, eng._ingest_stop = _StuckThread(), threading.Event()
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert "ingest" in ei.value.detail and "tick" not in ei.value.detail
    eng._ingest_thread = eng._ingest_stop = None


def test_switching_flag_spans_the_whole_swap_and_clears_on_every_exit(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    assert mgr.switching is False
    seen = []
    real_quiesce = state.engine.quiesce

    def _spy(timeout_s=5.0):
        seen.append(mgr.switching)
        return real_quiesce(timeout_s)
    monkeypatch.setattr(state.engine, "quiesce", _spy)
    mgr.activate("sim-1")
    # 停线程阶段就已经拒绝新请求。两次：等租约之前一次，等完租约之后对同一个引擎再停一次
    # （在途惰性构建可能在等租约期间补起线程，见 _quiesce_and_drain）。
    assert seen == [True, True]
    assert mgr.switching is False
    with pytest.raises(WorldSwitchError):    # world_not_found 路径：根本没置位过
        mgr.activate("nope")
    assert mgr.switching is False
    mgr.activate("sim-1")                    # 已是活动世界的 no-op
    assert mgr.switching is False


def test_activate_does_not_hold_engine_lock_while_joining_workers(tmp_path, monkeypatch):
    """后台线程每个循环会分几段各自拿引擎锁，段与段之间锁是空的：
    tick() 与 _sample_backpressure() 之间，或摄入逐块 remember() 之间。切换恰好卡在
    空档时，若 quiesce 是持着引擎锁 join，线程下一段拿不到锁、永远退不出，
    5 秒后误报 ingest_busy，而且这 5 秒里所有请求都排在引擎锁后面。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    eng = state.engine
    stop = threading.Event()
    in_first, go_first, gap = threading.Event(), threading.Event(), threading.Event()

    def _loop():
        with eng._lock:                 # 第一段：tick() / 一块 remember()
            in_first.set()
            go_first.wait(10)
        gap.wait(10)                    # 空档：锁是空的
        with eng._lock:                 # 第二段：_sample_backpressure() / 下一块 remember()
            pass
        stop.wait(10)

    worker = threading.Thread(target=_loop, daemon=True)
    eng._tick_thread, eng._tick_stop = worker, stop
    worker.start()
    assert in_first.wait(5)
    outcome: list = []
    done = threading.Event()

    def _switch():
        try:
            mgr.activate("sim-1")
            outcome.append("ok")
        except WorldSwitchError as exc:
            outcome.append(exc.code)
        finally:
            done.set()

    switcher = threading.Thread(target=_switch, daemon=True)
    switcher.start()
    try:
        time.sleep(0.2)                 # 切换线程此刻已在 quiesce 里 join worker（它没持引擎锁）；
                                        # worker 还卡在第一段里，join 要等它退出
        go_first.set()                  # 第一段结束，worker 进入空档
        time.sleep(0.2)
        gap.set()                       # worker 进入第二段，要拿引擎锁
        assert done.wait(3), "切换被持锁 join 拖住了（worker 在等切换线程释放引擎锁）"
        assert outcome == ["ok"], outcome
        assert not worker.is_alive() and state.config.tenant == "sim-1"
    finally:
        go_first.set()
        gap.set()
        stop.set()
        switcher.join(10)


def test_activate_waits_for_in_flight_request(tmp_path, monkeypatch):
    """在飞请求（持租约）结束之前，旧引擎不能被丢弃；结束之后旧 Runtime 必须真的被释放。
    请求不一定持着引擎锁（remember 的抽取段、converse 的生成段都在锁外），所以等的是
    租约而不是引擎锁。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    old_rt = weakref.ref(state.engine._rt)
    holding, release = threading.Event(), threading.Event()

    def _request():
        with mgr.lease() as eng:
            assert eng is state.engine
            holding.set()
            release.wait(10)
            del eng                      # 请求结束：引用随栈帧一起走

    req = threading.Thread(target=_request, daemon=True)
    req.start()
    assert holding.wait(5)
    done = threading.Event()
    switcher = threading.Thread(target=lambda: (mgr.activate("sim-1"), done.set()), daemon=True)
    switcher.start()
    # 不能只看「切换还没返回」：丢掉旧引擎后切换会在宽限期内等旧 Runtime 回收，同样
    # 不会立即返回。要看旧引擎此刻仍在 app.state 上——不等租约的实现会立即把它丢成 None。
    for _ in range(3):
        assert not done.wait(0.4), "请求还没结束，切换不该完成"
        assert state.engine is not None and state.config.tenant == "default"
        assert mgr.switching is True
    release.set()
    assert done.wait(10)
    req.join(5)
    assert state.config.tenant == "sim-1" and mgr.switching is False
    assert old_rt() is None, "等到请求结束才丢旧引擎，旧 Runtime 必须已释放"


def test_quiesce_stops_threads(tmp_path):
    cfg = _cfg(tmp_path)
    eng = DashboardEngine(cfg)
    eng.start_background_tick(60.0)
    eng.start_ingest_worker(poll_interval_s=60.0)
    assert eng.quiesce() is True
    assert eng._tick_thread is None and eng._ingest_thread is None


def test_quiesce_failure_keeps_stuck_worker_runnable(tmp_path):
    """join 超时：返回 False，句柄原样保留，且撤销停止信号——否则调用方回滚后
    这条线程一旦退出就再也没人重启，摄入悄悄停摆；句柄和事件不一致还会让
    之后的 stop_ingest_worker() 在 None.set() 上炸。"""
    eng = DashboardEngine(_cfg(tmp_path))

    class _Stuck:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    stuck, stop = _Stuck(), threading.Event()
    eng._ingest_thread, eng._ingest_stop = stuck, stop
    assert eng.quiesce() is False
    assert eng._ingest_thread is stuck and eng._ingest_stop is stop
    assert not stop.is_set()
    eng._ingest_thread = eng._ingest_stop = None     # 收尾，别留下假线程


def test_activate_does_not_start_workers_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.create("sim-1", "")
    mgr.activate("sim-1")                              # workers_enabled 默认 False
    assert state.engine._tick_thread is None and state.engine._ingest_thread is None


def test_activate_restarts_workers_when_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    seen = []
    mgr.on_tick = seen.append
    mgr.workers_enabled = True
    state.engine.start_background_tick(60.0, mgr.on_tick)
    state.engine.start_ingest_worker()
    mgr.create("sim-1", "")
    old = state.engine
    mgr.activate("sim-1")
    try:
        assert old._tick_thread is None and old._ingest_thread is None   # 旧线程已停
        assert state.engine._tick_thread is not None and state.engine._tick_thread.is_alive()
        assert state.engine._ingest_thread is not None and state.engine._ingest_thread.is_alive()
    finally:
        assert state.engine.quiesce() is True


def test_activate_without_engine_builds_one(tmp_path, monkeypatch):
    """惰性引擎（app.state.engine 还是 None）：切换照样能建出新世界的引擎。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    state = _State(cfg)
    mgr = WorldManager(state, bootstrap_worlds(cfg))
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    assert state.engine is not None and state.config.tenant == "sim-1"


def test_active_world_carries_name(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    mgr.create("sim-1", "一号")
    assert mgr.active_world()["name"] == "default"
    mgr.activate("sim-1")
    entry = mgr.active_world()
    assert entry["name"] == "sim-1" and entry["label"] == "一号" and entry["tenant"] == "sim-1"


def test_create_returns_entry_with_name_and_delete_protects_active(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    entry = mgr.create("sim-1", "一号")
    assert entry["name"] == "sim-1" and entry["tenant"] == "sim-1"
    assert os.path.isdir(os.path.dirname(entry["snapshot_dir"]))
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.create("sim-1", "")
    assert _code(ei.value) == "world_exists"
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.delete("default")
    assert _code(ei.value) == "world_protected"
    mgr.delete("sim-1")
    assert set(mgr.load()["worlds"]) == {"default"}
    assert not os.path.exists(os.path.dirname(entry["snapshot_dir"]))


def test_manager_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _remember(state.engine, "Bob owns auth")
    snap = mgr.take_snapshot("default", "手动")
    assert snap["kind"] == "manual" and snap["note"] == "手动"
    assert [s["id"] for s in mgr.load()["snapshots"]["default"]] == [snap["id"]]
    mgr.delete_snapshot("default", snap["id"])
    assert mgr.load()["snapshots"]["default"] == []
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.delete_snapshot("default", "nope")
    assert _code(ei.value) == "snapshot_not_found"


def test_snapshot_rejects_zvec_backend(tmp_path, monkeypatch):
    """拒绝由 C++ 给出（WorldStoreError），WorldManager 只把当前配置的后端传过去。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.vector_backend = "zvec"
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.take_snapshot("default", "")
    assert _code(ei.value) == "snapshot_unsupported_backend"
    assert mgr.load()["snapshots"].get("default", []) == []
    snap_dir = mgr.load()["worlds"]["default"]["snapshot_dir"]
    assert not os.path.isdir(snap_dir) or os.listdir(snap_dir) == []


def test_concurrent_snapshots_do_not_lose_entries(tmp_path, monkeypatch):
    """绑定在 VACUUM 期间释放 GIL，没有管理锁时两个线程各持旧 dict，后保存者覆盖先保存者。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    mgr.save({**mgr.load(), "snapshot_retention": 20})
    errors = []

    def _snap():
        try:
            mgr.take_snapshot("default", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_snap) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not errors
    listed = mgr.load()["snapshots"]["default"]
    assert len(listed) == 6
    assert len({s["id"] for s in listed}) == 6
    assert all(os.path.exists(s["path"]) for s in listed)


# ── POST /api/config：永远把默认世界写进 starling.json ──

_CFG_BODY = {"providers": {"default": {"provider": "openai", "model": "m",
                                       "base_url": "", "api_key": "sk-x"}},
             "roles": {"extraction": "default"}}


def _config_app(tmp_path, monkeypatch):
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: _core.FakeLLMAdapter())
    cfgfile = tmp_path / "starling.json"
    cfg = _cfg(tmp_path)
    cfg.config_path = str(cfgfile)
    eng = DashboardEngine(cfg)
    app = create_app(cfg, engine=eng)
    return app, cfgfile


def test_post_config_persists_default_world_while_other_world_active(tmp_path, monkeypatch):
    app, cfgfile = _config_app(tmp_path, monkeypatch)
    client = TestClient(app)                     # 不进 lifespan：切换不会起后台线程
    app.state.worlds.create("sim-1", "")
    app.state.worlds.activate("sim-1")
    assert app.state.config.tenant == "sim-1"
    assert client.post("/api/config", json=_CFG_BODY).status_code == 200
    saved = json.loads(cfgfile.read_text())
    assert saved["db_path"] == str(tmp_path / "dashboard.db")
    assert saved["tenant"] == "default"
    assert saved["providers"]["default"]["api_key"] == "sk-x"
    assert app.state.config.tenant == "sim-1"    # 运行时 config 不被改动
    assert client.delete("/api/config/provider/default").status_code == 200
    saved = json.loads(cfgfile.read_text())
    assert saved["db_path"] == str(tmp_path / "dashboard.db") and saved["tenant"] == "default"
    assert "default" not in saved["providers"]


def test_post_config_without_world_manager_saves_plain_config(tmp_path, monkeypatch):
    app, cfgfile = _config_app(tmp_path, monkeypatch)
    app.state.worlds = None
    client = TestClient(app)
    assert client.post("/api/config", json=_CFG_BODY).status_code == 200
    assert json.loads(cfgfile.read_text())["db_path"] == str(tmp_path / "dashboard.db")


def test_persistable_config_keeps_default_world(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    out = mgr.persistable_config(state.config)
    assert out.db_path == str(tmp_path / "dashboard.db")
    assert out.tenant == "default"
    assert state.config.tenant == "sim-1"      # 运行时 config 不被改动


# ───────────────────────── W1 Task 5：/api/worlds 路由 ─────────────────────────

def _client(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    return TestClient(app), app


def test_routes_list_create_activate_delete(tmp_path):
    client, app = _client(tmp_path)
    body = client.get("/api/worlds").json()
    assert body["active"] == "default" and body["snapshot_retention"] == 5
    assert [w["name"] for w in body["worlds"]] == ["default"]

    assert client.post("/api/worlds", json={"name": "Bad"}).json()["detail"] == "invalid_world_name"
    row = client.post("/api/worlds", json={"name": "sim-1", "label": "模拟"}).json()
    assert row["name"] == "sim-1" and row["active"] is False and row["engrams"] == 0
    assert client.post("/api/worlds", json={"name": "sim-1"}).status_code == 409

    r = client.post("/api/worlds/sim-1/activate")
    assert r.status_code == 200 and r.json() == {"active": "sim-1"}
    assert app.state.config.tenant == "sim-1"
    assert client.post("/api/worlds/nope/activate").status_code == 404

    r = client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"})
    assert r.status_code == 409 and r.json()["detail"] == "world_protected"
    client.post("/api/worlds/default/activate")
    r = client.delete("/api/worlds/sim-1", params={"confirm": "wrong"})
    assert r.status_code == 400 and r.json()["detail"] == "confirm_mismatch"
    r = client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"})
    assert r.status_code == 200
    assert [w["name"] for w in client.get("/api/worlds").json()["worlds"]] == ["default"]


def test_routes_snapshots(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)   # 建库
    snap = client.post("/api/worlds/default/snapshots", json={"note": "n1"}).json()
    assert snap["kind"] == "manual" and snap["note"] == "n1" and snap["size_bytes"] > 0
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    assert [s["id"] for s in listed] == [snap["id"]]
    assert client.get("/api/worlds").json()["worlds"][0]["snapshots"] == 1
    assert client.delete(f"/api/worlds/default/snapshots/{snap['id']}").status_code == 200
    assert client.delete("/api/worlds/default/snapshots/missing").status_code == 404
    assert client.post("/api/worlds/nope/snapshots", json={}).status_code == 404


def test_routes_snapshot_refused_for_zvec(tmp_path):
    client, app = _client(tmp_path)
    app.state.config.vector_backend = "zvec"
    r = client.post("/api/worlds/default/snapshots", json={})
    assert r.status_code == 409 and r.json()["detail"] == "snapshot_unsupported_backend"


def test_routes_require_token(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.token = "s3cret"
    client = TestClient(create_app(cfg))
    assert client.get("/api/worlds").status_code == 401
    assert client.get("/api/worlds", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_routes_never_accept_paths(tmp_path):
    client, _app = _client(tmp_path)
    # 名称校验本身就挡掉了路径；这里钉住它，防止将来放宽。
    for evil in ("../x", "a/b", "/abs"):
        assert client.post("/api/worlds", json={"name": evil}).status_code in (400, 404, 422)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/worlds"),
    ("post", "/api/worlds"),
    ("post", "/api/worlds/default/activate"),
    ("delete", "/api/worlds/default?confirm=default"),
    ("get", "/api/worlds/default/snapshots"),
    ("post", "/api/worlds/default/snapshots"),
    ("delete", "/api/worlds/default/snapshots/x"),
])
def test_routes_503_when_world_manager_absent(method, path):
    """:memory: 库没有注册表（app.state.worlds 为 None）：全部路由回 503，不是 AttributeError。"""
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    assert app.state.worlds is None
    kwargs = {"json": {"name": "sim-1"}} if (method, path) == ("post", "/api/worlds") else {}
    r = getattr(TestClient(app), method)(path, **kwargs)
    assert r.status_code == 503 and r.json()["detail"] == "worlds_unavailable"


def test_routes_worldrow_shape_and_stats(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    _remember(app.state.engine, "Bob is responsible for auth")
    client.post("/api/worlds", json={"name": "sim-1"})
    rows = {w["name"]: w for w in client.get("/api/worlds").json()["worlds"]}
    assert list(rows) == ["default", "sim-1"]
    keys = {"name", "label", "tenant", "created_at", "active", "db_bytes", "live_bytes",
            "statements", "engrams", "snapshots", "snapshot_bytes",
            "archives", "archive_bytes",                       # W3 契约新增
            "auto_snapshot"}                                   # W4 契约新增
    assert set(rows["default"]) == keys and set(rows["sim-1"]) == keys
    assert rows["default"]["active"] is True and rows["sim-1"]["active"] is False
    assert rows["default"]["engrams"] >= 1 and rows["default"]["db_bytes"] > 0
    assert rows["sim-1"]["engrams"] == 0 and rows["sim-1"]["db_bytes"] == 0   # 库未建
    assert rows["sim-1"]["tenant"] == "sim-1"


def test_routes_snapshot_rows_hide_server_paths_and_sum_bytes(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    first = client.post("/api/worlds/default/snapshots", json={"note": "a"}).json()
    second = client.post("/api/worlds/default/snapshots", json={"note": "b"}).json()
    assert set(first) == {"id", "kind", "reason", "note", "created_at", "world_time", "size_bytes",
                          "sessions_complete", "session_id"}           # W4 契约新增
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    stamps = [s["created_at"] for s in listed]
    assert stamps == sorted(stamps, reverse=True)            # 降序
    assert {s["id"] for s in listed} == {first["id"], second["id"]}
    row = client.get("/api/worlds").json()["worlds"][0]
    assert row["snapshots"] == 2
    assert row["snapshot_bytes"] == first["size_bytes"] + second["size_bytes"]


def test_routes_snapshot_body_is_optional(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    r = client.post("/api/worlds/default/snapshots")          # 无 body
    assert r.status_code == 200 and r.json()["note"] == ""


def test_routes_confirm_mismatch_does_not_touch_manager(tmp_path, monkeypatch):
    client, app = _client(tmp_path)
    client.post("/api/worlds", json={"name": "sim-1"})

    def _boom(*_a, **_k):
        raise AssertionError("manager must not be called")
    monkeypatch.setattr(app.state.worlds, "delete", _boom)
    assert client.delete("/api/worlds/sim-1").json()["detail"] == "confirm_mismatch"   # 缺 confirm
    r = client.delete("/api/worlds/sim-1", params={"confirm": "other"})
    assert r.status_code == 400 and r.json()["detail"] == "confirm_mismatch"


@pytest.mark.parametrize("code,status", [
    ("ingest_busy", 409), ("world_open_failed", 500), ("world_not_found", 404),
    ("snapshot_unsupported_backend", 409), ("world_switching", 503),
])
def test_routes_activate_maps_switch_errors(tmp_path, monkeypatch, code, status):
    client, app = _client(tmp_path)

    def _raise(_name):
        raise WorldSwitchError(code, "x")
    monkeypatch.setattr(app.state.worlds, "activate", _raise)
    r = client.post("/api/worlds/default/activate")
    assert r.status_code == status and r.json()["detail"] == code


def test_routes_snapshot_insufficient_disk_is_507(tmp_path, monkeypatch):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)

    def _no_space(*_args, **_kwargs):
        # C++ 的 free_space 钩子没有暴露给 Python，没法真造出磁盘不足；这里在绑定边界
        # 抛出同形异常（what() 形如 "<code>: <detail>"），只验证路由的 507 映射。
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
    monkeypatch.setattr(_core, "world_take_snapshot", _no_space)
    r = client.post("/api/worlds/default/snapshots", json={})
    assert r.status_code == 507 and r.json()["detail"] == "insufficient_disk"
    monkeypatch.undo()
    assert client.get("/api/worlds/default/snapshots").json()["snapshots"] == []   # 没留下条目


def test_routes_unknown_error_code_falls_back_to_500(tmp_path, monkeypatch):
    client, app = _client(tmp_path)

    def _raise(_name):
        raise WorldSwitchError("something_new")
    monkeypatch.setattr(app.state.worlds, "activate", _raise)
    r = client.post("/api/worlds/default/activate")
    assert r.status_code == 500 and r.json()["detail"] == "something_new"


def test_routes_broadcast_world_switched_and_snapshot_created(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            client.post("/api/worlds", json={"name": "sim-1"})
            assert client.post("/api/worlds/sim-1/activate").status_code == 200
            msg = ws.receive_json()
            assert msg == {"type": "world_switched", "payload": {"active": "sim-1"}}

            snap = client.post("/api/worlds/sim-1/snapshots", json={}).json()
            msg = ws.receive_json()
            assert msg == {"type": "snapshot_created",
                           "payload": {"world": "sim-1", "id": snap["id"]}}


def test_routes_failed_operations_do_not_broadcast(tmp_path):
    app = create_app(_cfg(tmp_path))
    sent = []

    async def _record(message):
        sent.append(message)
    app.state.ws_manager.broadcast = _record
    client = TestClient(app)
    assert client.post("/api/worlds/nope/activate").status_code == 404
    assert client.post("/api/worlds/nope/snapshots", json={}).status_code == 404
    assert sent == []


def test_routes_list_snapshots_unknown_world_is_404(tmp_path):
    client, _app = _client(tmp_path)
    r = client.get("/api/worlds/nope/snapshots")
    assert r.status_code == 404 and r.json()["detail"] == "world_not_found"
    # 已登记但从未拍过快照的世界：空列表，不是 404
    assert client.get("/api/worlds/default/snapshots").json() == {"snapshots": []}


# ── 切换窗口：engine=None 到新 engine 发布之间，请求不得惰性重建出旧配置的引擎 ──

def test_requests_get_503_world_switching_during_the_swap_window(tmp_path, monkeypatch):
    """窗口里 app.state.engine 是 None 而 app.state.config 还是旧的。_engine() 若此时
    惰性重建，会用旧配置造一个默认世界的引擎写回 app.state：activate 返回后，注册表和
    config 都说是 sim-1，真正的引擎却写着默认世界，activate 刚建好的引擎被丢成孤儿
    （它的 tick/摄入线程还在跑）。窗口内应当 503 world_switching。"""
    import starling.dashboard.engine as engmod
    import starling.dashboard.world_manager as wm
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    real = engmod.DashboardEngine
    building, proceed = threading.Event(), threading.Event()
    lazy_builds = []

    def _slow_open(cfg):
        if cfg.tenant == "sim-1":
            building.set()
            assert proceed.wait(15)
        return real(cfg)

    def _spy_lazy(cfg):
        lazy_builds.append(cfg.tenant)       # commands._engine 惰性重建走这里
        return real(cfg)
    monkeypatch.setattr(wm, "DashboardEngine", _slow_open)
    monkeypatch.setattr(engmod, "DashboardEngine", _spy_lazy)
    outcome: list = []
    switcher = threading.Thread(target=lambda: outcome.append(mgr.activate("sim-1")), daemon=True)
    switcher.start()
    try:
        assert building.wait(10)
        assert app.state.engine is None      # 窗口确实打开了：旧引擎已丢、新引擎未建好
        for method, path in (("post", "/api/tick"), ("get", "/api/runtime_health"),
                             ("get", "/api/ingest_status")):
            kwargs = {"json": {}} if method == "post" else {}
            r = getattr(client, method)(path, **kwargs)
            assert (r.status_code, r.json()["detail"]) == (503, "world_switching"), path
        assert app.state.engine is None and lazy_builds == []
    finally:
        proceed.set()
        switcher.join(15)
    assert outcome == [None]
    new = app.state.engine
    assert new is not None and new._cfg is app.state.config
    assert new._cfg.tenant == "sim-1" and lazy_builds == []
    assert client.post("/api/tick", json={}).status_code == 200      # 窗口一过，正常服务
    assert client.get("/api/runtime_health").status_code == 200
    assert new.quiesce() is True


def test_lazy_engine_build_still_works_when_not_switching(tmp_path):
    client, app = _client(tmp_path)
    assert app.state.engine is None
    assert client.post("/api/tick", json={}).status_code == 200
    assert app.state.engine is not None and app.state.engine._cfg is app.state.config


def test_lazy_build_and_switch_do_not_race_on_a_never_built_engine(tmp_path, monkeypatch):
    """engine 从未建过（惰性）时，切换开始的那一刻若有惰性构建正在进行，切换要等它
    建完再接手，而不是并行再建一个、互相覆盖。"""
    import starling.dashboard.engine as engmod
    client, app = _client(tmp_path)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    real = engmod.DashboardEngine
    in_build, proceed = threading.Event(), threading.Event()
    built = []

    def _slow(cfg):
        built.append(cfg.tenant)
        if len(built) == 1:
            in_build.set()
            assert proceed.wait(15)
        return real(cfg)
    monkeypatch.setattr(engmod, "DashboardEngine", _slow)
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "DashboardEngine", _slow)
    lazy = threading.Thread(target=lambda: client.post("/api/tick", json={}), daemon=True)
    lazy.start()
    assert in_build.wait(10)
    switch = threading.Thread(target=lambda: mgr.activate("sim-1"), daemon=True)
    switch.start()
    time.sleep(0.3)
    assert built == ["default"]              # 切换还在等惰性构建，没有并行再建
    proceed.set()
    lazy.join(15)
    switch.join(15)
    assert built == ["default", "sim-1"]
    assert app.state.engine._cfg.tenant == "sim-1" and app.state.config.tenant == "sim-1"
    assert app.state.engine.quiesce() is True


# ── 切换时先存注册表、后发布引擎；保存失败回到旧世界 ──

def _boom_save(_reg):
    raise OSError("No space left on device")


def test_activate_registry_save_failure_rolls_back_to_old_world(tmp_path, monkeypatch):
    """save 在引擎已发布之后才失败的旧顺序：activate 抛裸 OSError，注册表说 default 而
    config/engine 已是 sim-1，此后「切到 default」被当成 no-op，写入落进 sim-1，
    还能把正在写的 sim-1 目录删掉。新顺序：开库 -> 存注册表 -> 发布；存失败回滚。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    eng = state.engine
    eng.start_background_tick(60.0)
    eng.start_ingest_worker()
    _remember(eng, "Bob owns auth")
    mgr.create("sim-1", "")
    before = mgr.load()
    old_cfg = state.config
    built = []
    real_cls = DashboardEngine
    import starling.dashboard.world_manager as wm

    def _track(cfg):
        engine = real_cls(cfg)
        built.append((cfg.tenant, engine))
        return engine
    monkeypatch.setattr(wm, "DashboardEngine", _track)
    monkeypatch.setattr(mgr, "save", _boom_save)
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "world_open_failed"
    assert "registry write failed" in ei.value.detail and "No space left" in ei.value.detail
    monkeypatch.undo()
    assert state.config is old_cfg and state.config.tenant == "default"
    assert state.engine is not None and state.engine._cfg is old_cfg
    assert mgr.load() == before                              # 注册表原样
    assert mgr.switching is False
    # 被丢弃的 sim-1 引擎没起过线程；回滚回来的旧世界引擎线程在跑
    sim_engines = [e for tenant, e in built if tenant == "sim-1"]
    assert len(sim_engines) == 1
    assert sim_engines[0]._tick_thread is None and sim_engines[0]._ingest_thread is None
    assert state.engine._tick_thread is not None and state.engine._tick_thread.is_alive()
    assert state.engine._ingest_thread is not None and state.engine._ingest_thread.is_alive()
    _remember(state.engine, "Carol owns billing")            # 回滚回来的引擎能写
    # 之后一次正常的切换照样成功
    mgr.activate("sim-1")
    assert state.config.tenant == "sim-1" and mgr.load()["active"] == "sim-1"
    assert state.engine.quiesce() is True


def test_activate_save_failure_and_rollback_failure_both_reported(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    real_cls = wm.DashboardEngine
    calls = []

    def _second_build_fails(cfg):
        calls.append(cfg.tenant)
        if len(calls) == 2:                                   # 回滚重建旧世界
            raise RuntimeError("old db gone")
        return real_cls(cfg)
    monkeypatch.setattr(wm, "DashboardEngine", _second_build_fails)
    monkeypatch.setattr(mgr, "save", _boom_save)
    with pytest.raises(WorldSwitchError) as ei:
        mgr.activate("sim-1")
    assert ei.value.code == "world_open_failed"
    assert "registry write failed" in ei.value.detail and "old db gone" in ei.value.detail
    assert state.engine is None and state.config.tenant == "default"
    assert mgr.switching is False


def test_activate_noop_checks_live_config_not_just_registry(tmp_path, monkeypatch):
    """注册表说 default 是活动世界，但 live config 实际指着 sim-1（任何让两者分叉的
    路径）：切到 default 必须真的切，不能因为注册表这么说就当成 no-op。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    mgr.save(_core.world_set_active(mgr.load(), "default"))   # 人为造出分叉
    assert mgr.load()["active"] == "default" and state.config.tenant == "sim-1"
    old_engine = state.engine
    mgr.activate("default")
    assert state.engine is not old_engine
    assert state.config.tenant == "default"
    assert state.config.db_path == str(tmp_path / "dashboard.db")
    assert mgr.load()["active"] == "default"


def test_delete_refuses_the_world_the_live_engine_is_using(tmp_path, monkeypatch):
    """不信注册表的 active 字段：live config 指着的库，哪怕注册表说它不是活动世界，
    也不能删（macOS 上删目录后进程继续写 unlinked inode，重启即丢）。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    mgr.save(_core.world_set_active(mgr.load(), "default"))   # 注册表与 live config 分叉
    world_dir = os.path.dirname(state.config.db_path)
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.delete("sim-1")
    assert _code(ei.value) == "world_protected"
    assert os.path.isdir(world_dir) and "sim-1" in mgr.load()["worlds"]
    mgr.activate("default")
    mgr.delete("sim-1")                                       # 不再被使用后才能删
    assert not os.path.exists(world_dir)


def test_delete_of_an_unused_world_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    entry = mgr.create("sim-1", "")
    mgr.delete("sim-1")                                       # 没被使用：照常删
    assert not os.path.exists(os.path.dirname(entry["db_path"]))


def _tree_bytes(root: str) -> dict:
    """目录树里每个条目（不跟随符号链接）→ 普通文件的字节，其余记 None。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.isfile(path) and not os.path.islink(path):
                with open(path, "rb") as fh:
                    out[rel] = fh.read()
            else:
                out[rel] = None
    return out


def test_delete_refuses_a_world_dir_aliased_to_a_sibling_world_dir(tmp_path, monkeypatch):
    """worlds/sim-1 -> worlds/sim-2：旧规则只要求世界目录解析后在 worlds/ 之下，load 通过，
    delete("sim-1") 解析到 sim-2，把它的整个目录和库删掉（sim-2 不是活动世界，_same_db 拦不住）。
    现在世界目录解析后必须恰好是 worlds/<它自己的名字>：load 就报 registry_corrupt，路由回 500，
    sim-2 一个字节都不动，库照常可读。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    client, app = _client(tmp_path)
    mgr = app.state.worlds
    sim1_home = os.path.dirname(mgr.create("sim-1", "")["db_path"])
    sim2_db = mgr.create("sim-2", "")["db_path"]
    sim2_home = os.path.dirname(sim2_db)
    _migrated_world_db(mgr, "sim-2")
    _insert_foreign_engram(sim2_db, "sim-2")
    rows_before = _count(sim2_db, "engrams")
    assert rows_before == 1
    os.rmdir(os.path.join(sim1_home, "snapshots"))
    os.rmdir(sim1_home)
    os.symlink(sim2_home, sim1_home)
    tree_before = _tree_bytes(sim2_home)
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.delete("sim-1")
    assert _code(ei.value) == "registry_corrupt"
    assert "home of 'sim-1'" in str(ei.value) and "resolves to" in str(ei.value)
    del ei
    r = client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"})
    assert r.status_code == 500 and r.json()["detail"] == "registry_corrupt"
    assert _tree_bytes(sim2_home) == tree_before
    assert _count(sim2_db, "engrams") == rows_before
    assert os.path.islink(sim1_home)


def test_routes_non_domain_exception_is_json_500_internal_error(tmp_path, monkeypatch, caplog):
    client, app = _client(tmp_path)

    def _raise(_name):
        raise OSError("disk exploded at /secret/path")
    monkeypatch.setattr(app.state.worlds, "activate", _raise)
    with caplog.at_level("ERROR"):
        r = client.post("/api/worlds/default/activate")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "internal_error"}            # 不把异常文本（含路径）泄给客户端
    assert "disk exploded" in caplog.text                       # 服务端日志里有


def test_routes_list_survives_one_unreadable_world_db(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    created = client.post("/api/worlds", json={"name": "sim-1"}).json()
    assert created["name"] == "sim-1" and "stats_error" not in created
    db = app.state.worlds.load()["worlds"]["sim-1"]["db_path"]
    os.makedirs(os.path.dirname(db), exist_ok=True)
    with open(db, "wb") as fh:
        fh.write(b"this is not a sqlite database" * 100)
    r = client.get("/api/worlds")
    assert r.status_code == 200
    rows = {w["name"]: w for w in r.json()["worlds"]}
    assert rows["sim-1"]["stats_error"] is True
    assert (rows["sim-1"]["db_bytes"], rows["sim-1"]["live_bytes"],
            rows["sim-1"]["statements"], rows["sim-1"]["engrams"]) == (0, 0, 0, 0)
    assert rows["default"].get("stats_error", False) is False
    assert rows["default"]["db_bytes"] > 0                      # 好的世界照常有统计
    assert client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"}).status_code == 200
    assert [w["name"] for w in client.get("/api/worlds").json()["worlds"]] == ["default"]


# ───────────────────────── Codex 审查修复轮 ─────────────────────────

def _insert_foreign_engram(db_path: str, tenant: str) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
        "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
        "VALUES('x-1',?,'hx','conversation','default','inline','normal','audit_retain',"
        "'2026-10-25T00:00:00Z','x-1')", (tenant,))
    con.commit()
    con.close()


def _migrated_world_db(mgr, name: str) -> str:
    db = mgr.load()["worlds"][name]["db_path"]
    adapter = _core.SqliteAdapter.open(db)   # 跑迁移建库
    del adapter
    gc.collect()
    return db


# ── I6：zvec 拒绝在 C++ 里 ──

def test_binding_take_snapshot_refuses_non_sqlite_backend_in_cpp(tmp_path):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    _core.SqliteAdapter.open(reg["worlds"]["sim-1"]["db_path"])
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_take_snapshot(reg, "sim-1", vector_backend="zvec")
    assert _code(ei.value) == "snapshot_unsupported_backend"
    assert os.listdir(reg["worlds"]["sim-1"]["snapshot_dir"]) == []


# ── I1：租户不变式在激活时校验 ──

def test_activate_refuses_world_with_foreign_tenant_rows(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    _insert_foreign_engram(_migrated_world_db(mgr, "sim-1"), "intruder")
    engine, cfg = app.state.engine, app.state.config
    r = client.post("/api/worlds/sim-1/activate")
    assert (r.status_code, r.json()["detail"]) == (409, "world_tenant_mismatch")
    assert app.state.engine is engine and app.state.config is cfg     # 状态不前进
    assert mgr.load()["active"] == "default" and mgr.switching is False
    assert client.post("/api/tick", json={}).status_code == 200      # 旧世界照常服务


def test_binding_verify_tenant(tmp_path):
    db = str(tmp_path / "w.db")
    _core.world_verify_tenant(db, "sim-1")                          # 库不存在：新世界，放行
    _core.SqliteAdapter.open(db)
    _core.world_verify_tenant(db, "sim-1")
    _insert_foreign_engram(db, "intruder")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_verify_tenant(db, "sim-1")
    assert _code(ei.value) == "world_tenant_mismatch" and "engrams" in str(ei.value)


def test_activate_noop_also_compares_tenant(tmp_path, monkeypatch):
    """live config 指着同一个库、租户却与注册表不一致：不是「已激活」，要按注册表的租户
    重建引擎。只比 db_path 的话这次切换被当成 no-op，此后写入都带着错的租户。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    state.config.tenant = "drifted"                 # 人为造出租户分叉，库路径不变
    old_engine = state.engine
    mgr.activate("sim-1")
    assert state.engine is not old_engine
    assert state.config.tenant == "sim-1" and state.engine._core.tenant == "sim-1"


# ── I2 / I3：快照的崩溃残留与保存失败补偿 ──

def test_snapshot_save_failure_discards_new_file_and_is_json_500(tmp_path, monkeypatch):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    before = mgr.load()
    snap_dir = before["worlds"]["default"]["snapshot_dir"]
    monkeypatch.setattr(mgr, "save", _boom_save)
    r = client.post("/api/worlds/default/snapshots", json={})
    assert r.status_code == 500 and r.json() == {"detail": "internal_error"}
    monkeypatch.undo()
    assert mgr.load() == before
    leftovers = [n for n in os.listdir(snap_dir)] if os.path.isdir(snap_dir) else []
    assert leftovers == [], "注册表没存下来，新快照文件必须删掉，否则就是列不出来的孤儿"


def test_snapshot_sweeps_crash_leftovers(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    first = mgr.take_snapshot("default", "")
    snap_dir = os.path.dirname(first["path"])
    partial = os.path.join(snap_dir, "20260101T000000Z-dead.db.partial")
    orphan = os.path.join(snap_dir, "20260101T000000Z-beef.db")
    for path in (partial, orphan):
        with open(path, "w") as fh:
            fh.write("x")
    second = mgr.take_snapshot("default", "")
    assert not os.path.exists(partial) and not os.path.exists(orphan)
    assert os.path.exists(first["path"]) and os.path.exists(second["path"])


def test_routes_report_missing_snapshot_file_and_allow_delete(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    path = app.state.worlds.load()["snapshots"]["default"][0]["path"]
    os.remove(path)
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    assert len(listed) == 1
    assert listed[0]["missing"] is True and listed[0]["size_bytes"] == 0
    assert client.get("/api/worlds").json()["worlds"][0]["snapshot_bytes"] == 0
    assert client.delete(f"/api/worlds/default/snapshots/{snap['id']}").status_code == 200
    assert client.get("/api/worlds/default/snapshots").json() == {"snapshots": []}


# ── I5：切换窗口内所有检视路由都回 503 ──

def _force_switching(mgr, value: bool) -> None:
    with mgr._gate:
        mgr.switching = value


@pytest.mark.parametrize("path", ["/api/overview", "/api/statements", "/api/engrams",
                                  "/api/vitals", "/api/brain_map"])
def test_inspect_sql_routes_503_during_switch_window(tmp_path, path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    assert client.get(path).status_code == 200
    _force_switching(app.state.worlds, True)
    try:
        r = client.get(path)
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)


# ── I8：注入的引擎必须与引导后的 config 一致 ──

def test_create_app_rejects_engine_for_another_db(tmp_path):
    eng = DashboardEngine(DashboardConfig(db_path=str(tmp_path / "other.db"), token="",
                                          tick_interval_s=0, source_lane_enabled=False))
    with pytest.raises(ValueError) as ei:
        create_app(_cfg(tmp_path), engine=eng)
    assert "other.db" in str(ei.value) and "dashboard.db" in str(ei.value)


def test_create_app_rejects_engine_when_registry_overrides_shared_config(tmp_path):
    """生产形态的隐患：引擎与 create_app 共用同一个 cfg 对象，bootstrap 原地把它改成注册表
    里的活动世界，而引擎早已按改之前的路径开了库。比 cfg 字段永远相等，必须比引擎实际
    打开的库。"""
    create_app(_cfg(tmp_path))                        # 注册表：default -> dashboard.db
    cfg = _cfg(tmp_path)
    cfg.db_path = str(tmp_path / "elsewhere.db")
    eng = DashboardEngine(cfg)
    with pytest.raises(ValueError):
        create_app(cfg, engine=eng)


def test_create_app_accepts_matching_injected_engine(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg, engine=DashboardEngine(cfg))
    assert app.state.engine is not None


# ── C1：引擎租约覆盖整个请求 ──

def test_activate_times_out_on_stuck_lease_and_old_world_keeps_working(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.3)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    eng, cfg = state.engine, state.config
    eng.start_background_tick(60.0)
    eng.start_ingest_worker()
    mgr.create("sim-1", "")
    holding, release = threading.Event(), threading.Event()

    def _request():
        with mgr.lease():
            holding.set()
            release.wait(10)

    req = threading.Thread(target=_request, daemon=True)
    req.start()
    assert holding.wait(5)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            mgr.activate("sim-1")
        assert ei.value.code == "ingest_busy" and "1 request" in ei.value.detail
        assert state.engine is eng and state.config is cfg
        assert mgr.load()["active"] == "default" and mgr.switching is False
        assert eng._tick_thread is not None and eng._tick_thread.is_alive()
        assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
    finally:
        release.set()
        req.join(5)
    _remember(eng, "Bob owns auth")                    # 旧世界照常工作
    with mgr.lease() as again:                         # 窗口关了，新租约照常发
        assert again is eng
    assert eng.quiesce() is True


def test_new_request_during_lease_drain_gets_503(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    holding, release = threading.Event(), threading.Event()

    def _request():
        with mgr.lease():
            holding.set()
            release.wait(10)

    req = threading.Thread(target=_request, daemon=True)
    req.start()
    assert holding.wait(5)
    switcher = threading.Thread(target=lambda: mgr.activate("sim-1"), daemon=True)
    switcher.start()
    try:
        deadline = time.monotonic() + 5
        while not mgr.switching and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mgr.switching is True                   # 切换在等租约归零
        for method, path in (("post", "/api/tick"), ("post", "/api/recall"),
                             ("get", "/api/overview"), ("get", "/api/runtime_health")):
            kwargs = {"json": {"query": "x"}} if path == "/api/recall" else (
                {"json": {}} if method == "post" else {})
            r = getattr(client, method)(path, **kwargs)
            assert (r.status_code, r.json()["detail"]) == (503, "world_switching"), path
    finally:
        release.set()
        req.join(5)
        switcher.join(15)
    assert app.state.config.tenant == "sim-1"
    assert client.post("/api/tick", json={}).status_code == 200


def test_ws_converse_refused_during_switch_window_gets_world_switching_frame(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    app.state.engine.llm = fake
    _force_switching(app.state.worlds, True)
    try:
        with client.websocket_connect("/ws/converse") as ws:
            ws.send_json({"message": "who owns auth?"})
            assert ws.receive_json() == {"type": "error", "error": "world_switching"}
    finally:
        _force_switching(app.state.worlds, False)
    with client.websocket_connect("/ws/converse") as ws:     # 窗口一过照常对话
        ws.send_json({"message": "who owns auth?"})
        while (msg := ws.receive_json())["type"] in ("token", "status"):
            pass
        assert msg["type"] == "done"


def test_config_hot_swap_holds_a_lease(tmp_path, monkeypatch):
    app, _cfgfile = _config_app(tmp_path, monkeypatch)
    client = TestClient(app)
    _force_switching(app.state.worlds, True)
    try:
        r = client.post("/api/config", json=_CFG_BODY)
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
        r = client.delete("/api/config/provider/default")
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)
    assert client.post("/api/config", json=_CFG_BODY).status_code == 200


def test_slow_remember_spanning_a_switch_then_switching_back_leaves_one_runtime(
        tmp_path, monkeypatch):
    """remember 的抽取段在引擎锁外：旧实现只等持锁段，切换先返回，这条请求之后在旧引擎
    上提交，旧 Runtime 一直活着；再切回原世界就是同一个库上的两条写连接。租约让切换等
    整条请求结束。"""
    import starling.dashboard.world_manager as wm
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    fake.set_delay_ms(800)                              # 锁外抽取段拉长
    app.state.engine.llm = fake
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    runtimes = [weakref.ref(app.state.engine._rt)]
    real_cls = wm.DashboardEngine

    def _track(cfg):
        engine = real_cls(cfg)
        runtimes.append(weakref.ref(engine._rt))
        return engine
    monkeypatch.setattr(wm, "DashboardEngine", _track)
    del fake

    remembered: list = []
    req = threading.Thread(target=lambda: remembered.append(
        client.post("/api/remember", json={"text": "Bob owns auth"})), daemon=True)
    req.start()
    time.sleep(0.4)                                     # 已进锁外抽取段
    switched = threading.Event()
    switcher = threading.Thread(target=lambda: (mgr.activate("sim-1"), switched.set()),
                                daemon=True)
    switcher.start()
    assert not switched.wait(0.2), "remember 还在飞，切换不该完成"
    # 不等租约的实现此刻已把旧引擎丢成 None（只是在宽限期里等旧 Runtime 回收）。
    assert app.state.engine is not None and app.state.config.tenant == "default"
    req.join(15)
    assert switched.wait(15)
    switcher.join(5)
    assert remembered and remembered[0].status_code == 200
    gc.collect()
    assert runtimes[0]() is None, "请求结束后旧 Runtime 必须已释放"
    # 写入归属正确：切换前开始的请求写进旧世界
    default_db = mgr.load()["worlds"]["default"]["db_path"]
    con = sqlite3.connect(f"file:{default_db}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 1
    con.close()

    mgr.activate("default")                             # 切回原世界
    gc.collect()
    alive = [ref for ref in runtimes if ref() is not None]
    assert len(alive) == 1 and alive[0]() is app.state.engine._rt


# ── M2：converse 与 config 热更新整段持有租约（不是「查一下就还」） ──

def _wait_until(pred, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_switch_waits_for_an_in_flight_converse_turn(tmp_path, monkeypatch):
    """/ws/converse 的生成段在引擎锁外。租约若只在入口查一下就还，切换会在这一轮还在生成时
    完成：旧引擎被丢、这一轮随后在旧引擎上沉淀，旧 Runtime 一直活着。要求：生成被卡住时
    切换在等，放行后这一轮先完成、切换后完成，旧 Runtime 随之释放。"""
    import starling.dashboard.world_manager as wm
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    old_rt = weakref.ref(app.state.engine._rt)
    generating, release = threading.Event(), threading.Event()
    real_stream = DashboardEngine.converse_stream

    def _blocked_stream(self, message, **kwargs):
        generating.set()
        assert release.wait(10), "test never released the turn"
        return {"reply": "ok", "statement_ids": [], "remember_ok": True}
    monkeypatch.setattr(DashboardEngine, "converse_stream", _blocked_stream)

    frames: list = []

    def _turn():
        with client.websocket_connect("/ws/converse") as ws:
            ws.send_json({"message": "who owns auth?"})
            while True:
                msg = ws.receive_json()
                frames.append(msg)
                if msg["type"] in ("done", "error"):
                    return
    turn = threading.Thread(target=_turn, daemon=True)
    turn.start()
    assert generating.wait(5), "converse turn never reached generation"

    switched = threading.Event()
    switcher = threading.Thread(target=lambda: (mgr.activate("sim-1"), switched.set()),
                                daemon=True)
    switcher.start()
    try:
        assert _wait_until(lambda: mgr.switching), "switch never started"
        assert not switched.wait(0.3), "converse turn still generating; switch must wait"
        # 不等租约的实现此刻已经把旧引擎丢成 None（只是在宽限期里等旧 Runtime 回收）。
        assert app.state.engine is not None and app.state.config.tenant == "default"
    finally:
        release.set()
    turn.join(15)
    assert switched.wait(15)
    switcher.join(5)
    assert frames and frames[-1]["type"] == "done", frames
    assert app.state.config.tenant == "sim-1" and mgr.load()["active"] == "sim-1"
    monkeypatch.setattr(DashboardEngine, "converse_stream", real_stream)
    gc.collect()
    assert old_rt() is None, "the turn finished, so the old Runtime must be released"
    assert wm._LEASE_DRAIN_TIMEOUT_S > 0.3                  # 测试窗口远小于超时


def test_switch_waits_for_an_in_flight_config_hot_swap(tmp_path, monkeypatch):
    """config 热更新在线程池里换适配器（_apply）。租约若只在入口查一下就还，切换会在热更新
    还在跑时完成，适配器装到已被丢弃的旧引擎上。要求：_apply 被卡住时切换在等。"""
    app, _cfgfile = _config_app(tmp_path, monkeypatch)
    client = TestClient(app)
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    applying, release = threading.Event(), threading.Event()
    eng = app.state.engine
    real_set_llm = eng.set_llm

    def _blocked_set_llm(spec):
        applying.set()
        assert release.wait(10), "test never released the hot-swap"
        return real_set_llm(spec)
    monkeypatch.setattr(eng, "set_llm", _blocked_set_llm)
    del eng

    responses: list = []
    req = threading.Thread(target=lambda: responses.append(
        client.post("/api/config", json=_CFG_BODY)), daemon=True)
    req.start()
    assert applying.wait(5), "hot-swap never reached _apply"

    switched = threading.Event()
    switcher = threading.Thread(target=lambda: (mgr.activate("sim-1"), switched.set()),
                                daemon=True)
    switcher.start()
    try:
        assert _wait_until(lambda: mgr.switching), "switch never started"
        assert not switched.wait(0.3), "hot-swap still running; switch must wait"
        assert app.state.engine is not None and app.state.config.tenant == "default"
    finally:
        release.set()
    req.join(15)
    assert switched.wait(15)
    switcher.join(5)
    assert responses and responses[0].status_code == 200
    assert app.state.config.tenant == "sim-1"


# ── Codex 第二轮 Important 1：重启时活动世界的租户也要校验 ──

def test_restart_refuses_active_world_with_foreign_tenant_rows(tmp_path, monkeypatch):
    """切换时校验了租户，但重启时活动世界的引擎由 bootstrap 之后直接构造，绕过了校验。
    活动的非默认世界库里混进别的租户的行：重启必须拒绝，而不是带着它开始写。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    db = mgr.load()["worlds"]["sim-1"]["db_path"]
    del mgr, _state
    gc.collect()
    _insert_foreign_engram(db, "intruder")
    with pytest.raises(_core.WorldStoreError) as ei:
        bootstrap_worlds(_cfg(tmp_path))                 # 模拟重启
    assert _code(ei.value) == "world_tenant_mismatch"
    with pytest.raises(_core.WorldStoreError):
        create_app(_cfg(tmp_path))                       # 生产入口同样拒绝


def test_restart_on_a_clean_active_world_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    del mgr, _state
    gc.collect()
    cfg = _cfg(tmp_path)
    bootstrap_worlds(cfg)
    assert cfg.tenant == "sim-1"


def test_restart_does_not_tenant_check_the_legacy_default_world(tmp_path, monkeypatch):
    """默认世界（原地 dashboard.db）早于世界管理，既有部署可按租户过滤地存多个租户：
    重启时不因此拒绝（只校验活动的非默认世界）。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    bootstrap_worlds(cfg)
    eng = DashboardEngine(cfg)                           # 建库
    del eng
    gc.collect()
    _insert_foreign_engram(cfg.db_path, "other")
    again = _cfg(tmp_path)
    bootstrap_worlds(again)
    assert again.tenant == "default"


# ── Codex 第二轮 Minor ──

def test_quiesce_timeout_is_a_total_budget_for_both_threads(tmp_path, monkeypatch):
    """quiesce(timeout_s) 的参数原本没生效：两个 join 各写死 5 秒、串行，最坏 10 秒。
    两个线程都卡住时，总耗时必须受 timeout_s 约束。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    _mgr, state = _manager(tmp_path)
    eng = state.engine
    stuck = threading.Event()

    def _stuck_thread(name):
        thread = threading.Thread(target=stuck.wait, args=(30,), name=name, daemon=True)
        thread.start()
        return thread
    eng._ingest_thread, eng._ingest_stop = _stuck_thread("ingest"), threading.Event()
    eng._tick_thread, eng._tick_stop = _stuck_thread("tick"), threading.Event()
    try:
        started = time.monotonic()
        assert eng.quiesce(timeout_s=1.0) is False
        elapsed = time.monotonic() - started
        # 合计预算 1.0 秒；两次 join 各给满的旧行为约 2.0 秒。
        assert elapsed < 1.5, f"quiesce took {elapsed:.2f}s for a 1.0s total budget"
        assert set(eng.quiesce_stuck) == {"ingest", "tick"}
    finally:
        stuck.set()
        eng._ingest_thread.join(5)
        eng._tick_thread.join(5)


def test_get_config_503_during_switch_window(tmp_path, monkeypatch):
    app, _cfgfile = _config_app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/api/config").status_code == 200
    _force_switching(app.state.worlds, True)
    try:
        r = client.get("/api/config")
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)
    assert client.get("/api/config").status_code == 200


# ───────────────────────── W3：回滚语义 A（丢掉后续）与归档 ─────────────────────────

def _count(db_path: str, table: str = "statements") -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _checkpoint(db_path: str) -> None:
    """把 WAL 并回主文件并截成 0 字节。C++ 第 8 步只拒「-wal 非空」；先清空它，测试才能
    单独验证 Python 一侧的前提（库上没有还开着的写连接），不被 -wal 检查顺带挡住。"""
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        con.close()
    assert os.path.getsize(db_path + "-wal") == 0


def _write_snapshot_write(mgr, state, world: str) -> dict:
    """写 1 条 → 拍快照 → 再写 1 条：快照里 1 条语句，库里 2 条。"""
    _remember(state.engine, "Bob owns auth")
    snap = mgr.take_snapshot(world, "before Carol")
    _remember(state.engine, "Carol owns billing")
    return snap


def _enable_workers(mgr, state) -> None:
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    state.engine.start_background_tick(60.0)
    state.engine.start_ingest_worker()


def _rollback_outcome(mgr, name: str, snapshot_id: str):
    """不用 pytest.raises：要先断言库文件没被替换，再看错误码，回退验证时失败信息才指向
    「文件被换了」。"""
    try:
        return mgr.rollback(name, snapshot_id), None
    except (WorldSwitchError, _core.WorldStoreError) as exc:
        return None, exc


def test_binding_rollback_roundtrip_archives_and_error_codes(tmp_path):
    rd = str(tmp_path)                            # 注册表目录：世界 home 与归档目录只从它推出
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, rd, "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    db = reg["worlds"]["sim-1"]["db_path"]
    adapter = _core.SqliteAdapter.open(db)        # 跑迁移建库
    del adapter
    gc.collect()
    out = _core.world_take_snapshot(reg, "sim-1")
    reg, snap = out["registry"], out["snapshot"]
    _insert_foreign_engram(db, "sim-1")           # 快照之后的写入（本租户）
    assert _count(db, "engrams") == 1
    frozen = json.dumps(reg, sort_keys=True)

    result = _core.world_rollback(reg, rd, "sim-1", snap["id"])
    assert set(result) == {"archive_path", "restored_bytes"}
    assert result["restored_bytes"] == snap["size_bytes"]
    home = os.path.dirname(reg["worlds"]["sim-1"]["snapshot_dir"])
    assert os.path.dirname(result["archive_path"]) == os.path.join(home, "archive")
    assert _count(db, "engrams") == 0                                  # 回到快照那一刻
    con = sqlite3.connect(f"file:{result['archive_path']}?mode=ro&immutable=1", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 1   # 旧库原样在归档里
    con.close()
    assert json.dumps(reg, sort_keys=True) == frozen                   # 入参不被修改

    listed = _core.world_list_archives(reg, rd, "sim-1")
    assert listed == [{"name": os.path.basename(result["archive_path"]),
                       "size_bytes": os.path.getsize(result["archive_path"])}]
    assert _core.world_purge_archives(reg, rd, "sim-1") == 1
    assert _core.world_list_archives(reg, rd, "sim-1") == []

    for call, code in (
            (lambda: _core.world_rollback(reg, rd, "sim-1", "nope"), "snapshot_not_found"),
            (lambda: _core.world_rollback(reg, rd, "nope", snap["id"]), "world_not_found"),
            (lambda: _core.world_rollback(reg, rd, "sim-1", snap["id"], vector_backend="zvec"),
             "snapshot_unsupported_backend"),
            (lambda: _core.world_list_archives(reg, rd, "nope"), "world_not_found"),
            (lambda: _core.world_purge_archives(reg, rd, "nope"), "world_not_found")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _code(ei.value) == code


def test_rollback_inactive_world_restores_snapshot_and_keeps_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    snap = _write_snapshot_write(mgr, state, "sim-1")
    mgr.activate("default")
    db = mgr.load()["worlds"]["sim-1"]["db_path"]
    assert _count(db) == 2
    before = mgr.load()
    engine, cfg = state.engine, state.config
    out = mgr.rollback("sim-1", snap["id"])
    assert out == {"world": "sim-1", "snapshot_id": snap["id"], "archived": True, "active": False}
    assert _count(db) == 1
    assert mgr.load() == before                                # 回滚不写注册表
    assert state.engine is engine and state.config is cfg      # 活动世界的引擎没被动
    archives = mgr.archives("sim-1")
    assert len(archives) == 1 and archives[0]["size_bytes"] > 0
    home = os.path.dirname(before["worlds"]["sim-1"]["snapshot_dir"])
    assert os.listdir(os.path.join(home, "archive")) == [archives[0]["name"]]
    del engine
    mgr.activate("sim-1")                                      # 恢复出来的库能正常激活、写入
    _remember(state.engine, "Dave owns search")
    assert _count(db) == 2


def test_rollback_active_world_rebuilds_engine_and_restarts_workers(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _enable_workers(mgr, state)
    snap = _write_snapshot_write(mgr, state, "default")
    db, cfg = state.config.db_path, state.config
    assert _count(db) == 2
    old_rt = weakref.ref(state.engine._rt)
    old_threads = (state.engine._tick_thread, state.engine._ingest_thread)
    inode = os.stat(db).st_ino
    out = mgr.rollback("default", snap["id"])
    assert out == {"world": "default", "snapshot_id": snap["id"], "archived": True, "active": True}
    assert old_rt() is None, "旧 Runtime 必须在动文件之前已被回收"
    assert all(t is not None and not t.is_alive() for t in old_threads)
    new = state.engine
    assert new is not None and new._cfg is cfg and state.config is cfg   # config 不变
    assert mgr.switching is False and mgr.load()["active"] == "default"
    assert os.stat(db).st_ino != inode                                   # 库换成了快照的内容
    assert _count(db) == 1
    assert new._tick_thread is not None and new._tick_thread.is_alive()
    assert new._ingest_thread is not None and new._ingest_thread.is_alive()
    _remember(new, "Dave owns search")                                   # 回滚后照常写入
    assert _count(db) == 2
    assert len(mgr.archives("default")) == 1
    assert new.quiesce() is True


def test_rollback_active_world_with_corrupt_snapshot_reopens_the_untouched_db(
        tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _enable_workers(mgr, state)
    snap = _write_snapshot_write(mgr, state, "default")
    db = state.config.db_path
    with open(snap["path"], "wb") as fh:
        fh.write(b"this is not a sqlite database" * 512)
    old_rt = weakref.ref(state.engine._rt)
    inode = os.stat(db).st_ino
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.rollback("default", snap["id"])
    assert _code(ei.value) == "snapshot_corrupt"                  # C++ 的码原样抛出
    assert state.engine is not None, "C++ 失败后必须按原 config 重建引擎"
    assert old_rt() is None and state.engine._rt is not None
    assert os.stat(db).st_ino == inode and _count(db) == 2        # 目标库未动
    assert mgr.archives("default") == []
    assert mgr.switching is False
    assert state.engine._tick_thread is not None and state.engine._tick_thread.is_alive()
    assert state.engine._ingest_thread is not None and state.engine._ingest_thread.is_alive()
    _remember(state.engine, "Dave owns search")                   # 重建出来的引擎能写
    assert _count(db) == 3
    assert state.engine.quiesce() is True


@pytest.mark.parametrize("corrupt", [False, True])
def test_rollback_active_world_reopen_failure_code_says_whether_the_db_changed(
        tmp_path, monkeypatch, corrupt):
    """回滚之后按原 config 重建引擎失败：engine 留 None、窗口已关；之后惰性重建得到的是同一个
    库。码按库有没有被替换来分（B4）：替换成功之后是 rollback_reopen_failed（链到重建的异常，
    库已是快照内容）；C++ 失败之后是 world_open_failed（链到 C++ 的错误，库未动）。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db, cfg = state.config.db_path, state.config
    if corrupt:
        with open(snap["path"], "wb") as fh:
            fh.write(b"not sqlite" * 512)

    def _fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _fail)
    with pytest.raises(WorldSwitchError) as ei:
        mgr.rollback("default", snap["id"])
    assert "disk on fire" in ei.value.detail
    assert state.engine is None and state.config is cfg and mgr.switching is False
    if corrupt:
        assert ei.value.code == "world_open_failed"
        assert isinstance(ei.value.__cause__, _core.WorldStoreError)
        assert _code(ei.value.__cause__) == "snapshot_corrupt"
        assert _count(db) == 2                                    # 目标库未动
    else:
        assert ei.value.code == "rollback_reopen_failed"
        assert isinstance(ei.value.__cause__, RuntimeError)
        assert "rollback applied" in ei.value.detail and _count(db) == 1
    monkeypatch.undo()
    assert DashboardEngine(cfg).health().name == "READY"          # 库本身好好的


def test_rollback_active_world_refuses_while_old_runtime_is_still_referenced(
        tmp_path, monkeypatch, caplog):
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.1)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db = state.config.db_path
    keep = state.engine._rt                  # 故意钉住旧 Runtime：它的写连接一直开着
    _checkpoint(db)
    inode = os.stat(db).st_ino
    with caplog.at_level("WARNING"):
        out, err = _rollback_outcome(mgr, "default", snap["id"])
    assert os.stat(db).st_ino == inode, "旧 Runtime 还开着写连接，库文件却被替换了"
    assert out is None and isinstance(err, WorldSwitchError)
    assert err.code == "ingest_busy" and "old runtime" in err.detail
    assert "old runtime still referenced" in caplog.text
    assert _count(db) == 2 and mgr.archives("default") == []
    assert state.engine is not None and state.engine._rt is not keep   # 按原 config 重建
    assert mgr.switching is False
    _remember(state.engine, "Dave owns search")                         # 引擎恢复可用
    assert _count(db) == 3
    del keep
    gc.collect()
    out = mgr.rollback("default", snap["id"])            # 旧 Runtime 回收之后照常回滚
    assert out["active"] is True and _count(db) == 1


def test_rollback_refuses_world_whose_runtime_leaked_from_an_earlier_switch(
        tmp_path, monkeypatch, caplog):
    """切换时旧 Runtime 没回收只记 warning。它还开着那个库的写连接：库若被回滚替换，这条连接
    之后的写入会落进旧 inode 和它已被 unlink 的 WAL，恢复出来的库里没有，静默丢失，归档也可能
    被改动（关掉它时 SQLite 发现主文件已被换掉，什么都不做）。所以回滚非活动世界也要先查。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.1)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db = state.config.db_path
    mgr.create("sim-1", "")
    keep = state.engine._rt
    with caplog.at_level("WARNING"):
        mgr.activate("sim-1")
    assert "old runtime still referenced" in caplog.text      # 切换照旧只告警、照常完成
    assert state.config.tenant == "sim-1"
    _checkpoint(db)
    inode = os.stat(db).st_ino
    out, err = _rollback_outcome(mgr, "default", snap["id"])
    assert os.stat(db).st_ino == inode, "旧 Runtime 还开着写连接，库文件却被替换了"
    assert out is None and isinstance(err, WorldSwitchError)
    assert err.code == "ingest_busy" and "old runtime" in err.detail
    assert _count(db) == 2 and mgr.archives("default") == []
    assert state.config.tenant == "sim-1" and mgr.switching is False
    del keep
    gc.collect()
    out = mgr.rollback("default", snap["id"])
    assert out == {"world": "default", "snapshot_id": snap["id"], "archived": True,
                   "active": False}
    assert _count(db) == 1


def test_rollback_active_world_times_out_on_stuck_lease(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.3)
    mgr, state = _manager(tmp_path)
    _enable_workers(mgr, state)
    snap = _write_snapshot_write(mgr, state, "default")
    db = state.config.db_path
    eng, cfg = state.engine, state.config
    inode = os.stat(db).st_ino
    holding, release = threading.Event(), threading.Event()

    def _request():
        with mgr.lease():
            holding.set()
            release.wait(10)

    req = threading.Thread(target=_request, daemon=True)
    req.start()
    assert holding.wait(5)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            mgr.rollback("default", snap["id"])
        assert ei.value.code == "ingest_busy" and "1 request" in ei.value.detail
        assert state.engine is eng and state.config is cfg          # 引擎没丢
        assert os.stat(db).st_ino == inode and _count(db) == 2      # 文件未动
        assert mgr.archives("default") == [] and mgr.switching is False
        assert eng._tick_thread is not None and eng._tick_thread.is_alive()
        assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
    finally:
        release.set()
        req.join(5)
    assert eng.quiesce() is True


def test_rollback_inactive_world_refuses_zvec_backend_in_cpp(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    mgr.create("sim-1", "")
    mgr.activate("sim-1")
    snap = _write_snapshot_write(mgr, state, "sim-1")
    mgr.activate("default")
    db = mgr.load()["worlds"]["sim-1"]["db_path"]
    inode = os.stat(db).st_ino
    state.config.vector_backend = "zvec"
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.rollback("sim-1", snap["id"])
    assert _code(ei.value) == "snapshot_unsupported_backend"
    assert os.stat(db).st_ino == inode and _count(db) == 2 and mgr.archives("sim-1") == []


def test_rollback_active_world_refuses_zvec_backend_before_touching_the_engine(
        tmp_path, monkeypatch):
    """拒绝由 C++ 给出（spec §12），在停引擎之前的预检里（C1）：引擎不丢、不重建。本机的 _core
    不是 zvec 构建，真按 zvec 建不起引擎，所以替身记下调用、实际按 sqlite 建。"""
    import dataclasses

    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db, eng = state.config.db_path, state.engine
    inode = os.stat(db).st_ino
    state.config.vector_backend = "zvec"
    asked = []
    real = wm.DashboardEngine

    def _build(cfg):
        asked.append(cfg)
        return real(dataclasses.replace(cfg, vector_backend="sqlite"))
    monkeypatch.setattr(wm, "DashboardEngine", _build)
    out, err = _rollback_outcome(mgr, "default", snap["id"])
    assert state.engine is eng and asked == [], "预检失败却拆掉、重建了引擎"
    assert os.stat(db).st_ino == inode and _count(db) == 2 and mgr.archives("default") == []
    assert mgr.switching is False
    assert out is None and isinstance(err, _core.WorldStoreError)
    assert _code(err) == "snapshot_unsupported_backend"


def test_rollback_of_the_live_world_takes_the_switch_window_even_without_an_engine(
        tmp_path, monkeypatch):
    """「活动」看 live config 指着哪个库，不看引擎在不在：engine 还是 None 时，窗口外的
    惰性构建会在回滚中途打开这个库。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    state = _State(cfg)
    mgr = WorldManager(state, bootstrap_worlds(cfg))
    state.engine = DashboardEngine(cfg)
    snap = _write_snapshot_write(mgr, state, "default")
    rt = weakref.ref(state.engine._rt)
    state.engine = None                                   # 回到「从未建过引擎」的形态
    gc.collect()
    assert rt() is None
    seen = []
    real = _core.world_rollback

    def _spy(*args, **kwargs):
        seen.append(mgr.switching)
        return real(*args, **kwargs)
    monkeypatch.setattr(_core, "world_rollback", _spy)
    out = mgr.rollback("default", snap["id"])
    assert out["active"] is True and seen == [True]
    assert state.engine is not None and state.engine._cfg is cfg
    assert _count(cfg.db_path) == 1 and mgr.switching is False


def test_rollback_unknown_world_is_world_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, _state = _manager(tmp_path)
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.rollback("nope", "x")
    assert _code(ei.value) == "world_not_found"


def _rollback_body(world: str, snapshot_id: str) -> dict:
    return {"snapshot_id": snapshot_id, "confirm": world, "mode": "discard_after"}


def test_routes_rollback_200_broadcast_and_archives(tmp_path):
    app = create_app(_cfg(tmp_path))
    app.state.engine = DashboardEngine(app.state.config)
    with TestClient(app) as client:
        _remember(app.state.engine, "Bob owns auth")
        snap = client.post("/api/worlds/default/snapshots", json={}).json()
        _remember(app.state.engine, "Carol owns billing")
        with client.websocket_connect("/ws") as ws:
            r = client.post("/api/worlds/default/rollback",
                            json=_rollback_body("default", snap["id"]))
            assert r.status_code == 200, r.json()
            assert r.json() == {"world": "default", "snapshot_id": snap["id"],
                                "archived": True, "active": True}
            assert ws.receive_json() == {"type": "world_rolled_back",
                                         "payload": {"world": "default", "snapshot_id": snap["id"]}}
        row = client.get("/api/worlds").json()["worlds"][0]
        assert row["statements"] == 1
        assert row["archives"] == 1 and row["archive_bytes"] > 0
        r = client.delete("/api/worlds/default/archives", params={"confirm": "default"})
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        row = client.get("/api/worlds").json()["worlds"][0]
        assert (row["archives"], row["archive_bytes"]) == (0, 0)
        r = client.delete("/api/worlds/nope/archives", params={"confirm": "nope"})
        assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")


def test_routes_rollback_mode_defaults_to_discard_after(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    r = client.post("/api/worlds/default/rollback",
                    json={"snapshot_id": snap["id"], "confirm": "default"})
    assert r.status_code == 200 and r.json()["archived"] is True


def test_routes_rollback_and_purge_check_confirm_and_mode_before_the_manager(
        tmp_path, monkeypatch):
    client, app = _client(tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("manager must not be called")
    monkeypatch.setattr(app.state.worlds, "rollback", _boom)
    monkeypatch.setattr(app.state.worlds, "purge_archives", _boom)
    url = "/api/worlds/default/rollback"
    for body in ({"snapshot_id": "x"}, {"snapshot_id": "x", "confirm": "other"}):
        r = client.post(url, json=body)
        assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")
    r = client.post(url, json={"snapshot_id": "x", "confirm": "default", "mode": "scene"})
    assert (r.status_code, r.json()["detail"]) == (400, "rollback_mode_unsupported")
    for params in ({}, {"confirm": "other"}):
        r = client.delete("/api/worlds/default/archives", params=params)
        assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")


def test_routes_rollback_not_found_codes(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", "missing"))
    assert (r.status_code, r.json()["detail"]) == (404, "snapshot_not_found")
    r = client.post("/api/worlds/nope/rollback", json=_rollback_body("nope", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    assert client.post("/api/tick", json={}).status_code == 200      # 引擎照常可用


def test_routes_rollback_to_a_missing_snapshot_file_is_409_snapshot_corrupt(tmp_path):
    """前端不给缺失的快照显示回滚按钮，但后端必须自己拒：快照文件不在 → snapshot_corrupt。"""
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    _remember(app.state.engine, "Bob owns auth")
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    _remember(app.state.engine, "Carol owns billing")
    os.remove(app.state.worlds.load()["snapshots"]["default"][0]["path"])
    db = app.state.config.db_path
    inode = os.stat(db).st_ino
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (409, "snapshot_corrupt")
    assert os.stat(db).st_ino == inode and _count(db) == 2
    assert client.post("/api/tick", json={}).status_code == 200


def test_routes_rollback_during_switch_window_is_503(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    _remember(app.state.engine, "Bob owns auth")
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    _remember(app.state.engine, "Carol owns billing")
    _force_switching(app.state.worlds, True)
    try:
        r = client.post("/api/worlds/default/rollback",
                        json=_rollback_body("default", snap["id"]))
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)
    assert _count(app.state.config.db_path) == 2


@pytest.mark.parametrize("source,code,status", [
    ("cpp", "snapshot_corrupt", 409), ("cpp", "world_tenant_mismatch", 409),
    ("cpp", "insufficient_disk", 507), ("cpp", "snapshot_failed", 500),
    ("cpp", "snapshot_unsupported_backend", 409), ("cpp", "world_in_use", 409),
    ("manager", "ingest_busy", 409), ("manager", "world_open_failed", 500),
])
def test_routes_rollback_maps_error_codes_and_does_not_broadcast(tmp_path, monkeypatch,
                                                                source, code, status):
    app = create_app(_cfg(tmp_path))
    sent = []

    async def _record(message):
        sent.append(message)
    app.state.ws_manager.broadcast = _record

    def _raise(_name, _snapshot_id):
        if source == "cpp":
            raise _core.WorldStoreError(f"{code}: x")     # 与 C++ what() 同形
        raise WorldSwitchError(code, "x")
    monkeypatch.setattr(app.state.worlds, "rollback", _raise)
    r = TestClient(app).post("/api/worlds/default/rollback", json=_rollback_body("default", "s"))
    assert (r.status_code, r.json()["detail"]) == (status, code)
    assert sent == []


@pytest.mark.parametrize("method,path", [
    ("post", "/api/worlds/default/rollback"),
    ("delete", "/api/worlds/default/archives?confirm=default"),
])
def test_rollback_routes_503_when_world_manager_absent(method, path):
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    kwargs = {"json": _rollback_body("default", "s")} if method == "post" else {}
    r = getattr(TestClient(app), method)(path, **kwargs)
    assert r.status_code == 503 and r.json()["detail"] == "worlds_unavailable"


def test_routes_created_world_row_carries_zero_archives(tmp_path):
    client, _app = _client(tmp_path)
    row = client.post("/api/worlds", json={"name": "sim-1"}).json()
    assert (row["archives"], row["archive_bytes"]) == (0, 0)


def test_routes_unreadable_archive_dir_is_zero_archives_not_stats_error(tmp_path, caplog):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    home = os.path.dirname(app.state.worlds.load()["worlds"]["default"]["snapshot_dir"])
    os.makedirs(home, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(home, "archive"))   # 解析后越出世界 home：C++ 拒列
    with caplog.at_level("WARNING"):
        r = client.get("/api/worlds")
    assert r.status_code == 200
    row = r.json()["worlds"][0]
    assert (row["archives"], row["archive_bytes"]) == (0, 0)
    assert "stats_error" not in row and row["db_bytes"] > 0      # 库本身读得出
    assert "world_list_archives failed" in caplog.text


# ───────────────────────── W3 最终修复轮 ─────────────────────────

# ── A3：活动世界回滚的两条失败出口 ──

def test_rollback_weakref_refusal_then_reopen_failure_is_world_open_failed(tmp_path, monkeypatch):
    """弱引用拒绝（旧 Runtime 还开着写连接，库未动）之后重建引擎也失败：仍是 world_open_failed
    （库没被替换），链到 ingest_busy；engine 留 None，窗口已关。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.1)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db, cfg = state.config.db_path, state.config
    keep = state.engine._rt                  # 钉住旧 Runtime：弱引用拒绝
    inode = os.stat(db).st_ino

    def _fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _fail)
    out, err = _rollback_outcome(mgr, "default", snap["id"])
    assert os.stat(db).st_ino == inode, "旧 Runtime 还开着写连接，库文件却被替换了"
    assert out is None and isinstance(err, WorldSwitchError)
    assert err.code == "world_open_failed" and "disk on fire" in err.detail
    assert isinstance(err.__cause__, WorldSwitchError) and err.__cause__.code == "ingest_busy"
    assert state.engine is None and state.config is cfg and mgr.switching is False
    assert _count(db) == 2 and mgr.archives("default") == []
    del keep
    gc.collect()


def test_rollback_active_world_refuses_when_workers_will_not_stop(tmp_path, monkeypatch):
    """quiesce 失败（后台线程 5 秒内没停下）：ingest_busy，库未动，引擎原样保留且线程已恢复。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _enable_workers(mgr, state)
    snap = _write_snapshot_write(mgr, state, "default")
    db = state.config.db_path
    eng, cfg = state.engine, state.config
    inode = os.stat(db).st_ino

    def _stopped_but_reports_busy(timeout_s=5.0):
        eng.stop_ingest_worker()
        eng.stop_background_tick()
        return False
    monkeypatch.setattr(eng, "quiesce", _stopped_but_reports_busy)
    out, err = _rollback_outcome(mgr, "default", snap["id"])
    assert os.stat(db).st_ino == inode
    assert out is None and isinstance(err, WorldSwitchError) and err.code == "ingest_busy"
    assert state.engine is eng and state.config is cfg          # 同一个引擎，没丢
    assert mgr.switching is False
    assert eng._tick_thread is not None and eng._tick_thread.is_alive()
    assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
    assert _count(db) == 2 and mgr.archives("default") == []
    monkeypatch.undo()
    assert eng.quiesce() is True


# ── B2：打开活动世界库的路由持租约 ──

def test_rollback_waits_for_an_in_flight_inspect_query(tmp_path, monkeypatch):
    """检视 SQL 路由持租约直到连接关闭：一条打开了旧 inode、还没读的只读连接（C++ 的
    world_in_use 探测看不见它）在飞时，回滚等不到租约归零 → ingest_busy，库未动。它结束之后
    回滚照常成功。不持租约的话回滚会在它还开着时替换库文件。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.3)
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    _remember(app.state.engine, "Bob owns auth")
    snap = mgr.take_snapshot("default", "")
    _remember(app.state.engine, "Carol owns billing")
    db = app.state.config.db_path
    real = queries.overview
    opened, release = threading.Event(), threading.Event()

    def _blocked(db_path, tenant):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)   # 打开了，还没读
        try:
            opened.set()
            assert release.wait(10)
        finally:
            con.close()
        return real(db_path, tenant)
    monkeypatch.setattr(queries, "overview", _blocked)
    results = []
    req = threading.Thread(target=lambda: results.append(client.get("/api/overview")),
                           daemon=True)
    req.start()
    assert opened.wait(5)
    inode = os.stat(db).st_ino
    try:
        out, err = _rollback_outcome(mgr, "default", snap["id"])
        assert os.stat(db).st_ino == inode, "检视查询还开着库，库文件却被替换了"
        assert out is None and isinstance(err, WorldSwitchError) and err.code == "ingest_busy"
        assert mgr.switching is False and app.state.engine is not None
    finally:
        release.set()
        req.join(10)
    assert results and results[0].status_code == 200
    # err 的回溯钉着 _quiesce_and_drain 的栈帧，帧里的局部变量引用着引擎（旧 Runtime）。
    err = None
    out = mgr.rollback("default", snap["id"])
    assert out["active"] is True and os.stat(db).st_ino != inode and _count(db) == 1


def test_routes_list_worlds_503_during_switch_window(tmp_path):
    """GET /api/worlds 的 world_stats 会打开活动世界的库，同样持租约：窗口内 503。"""
    client, app = _client(tmp_path)
    assert client.get("/api/worlds").status_code == 200
    _force_switching(app.state.worlds, True)
    try:
        r = client.get("/api/worlds")
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)
    assert client.get("/api/worlds").status_code == 200


# ── B3：C++ 探测别的连接（world_in_use） ──

_READER = (
    "import sqlite3, sys\n"
    "con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)\n"
    "print(con.execute('SELECT COUNT(*) FROM engrams').fetchone()[0], flush=True)\n"
    "sys.stdin.read()\n"
    "con.close()\n")


def test_binding_rollback_refuses_db_open_in_another_process(tmp_path):
    rd = str(tmp_path)
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, rd, "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    db = _migrated_world_db_at(reg, "sim-1")
    out = _core.world_take_snapshot(reg, "sim-1")
    reg, snap = out["registry"], out["snapshot"]
    _insert_foreign_engram(db, "sim-1")
    proc = subprocess.Popen([sys.executable, "-I", "-c", _READER, db], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "1"          # 另一个进程读过了，连接开着
        with open(db, "rb") as fh:
            before = fh.read()
        inode = os.stat(db).st_ino
        with pytest.raises(_core.WorldStoreError) as ei:
            _core.world_rollback(reg, rd, "sim-1", snap["id"])
        assert _code(ei.value) == "world_in_use"
        assert os.stat(db).st_ino == inode
        with open(db, "rb") as fh:
            assert fh.read() == before
        assert _core.world_list_archives(reg, rd, "sim-1") == []
    finally:
        proc.stdin.close()
        proc.wait(10)
    result = _core.world_rollback(reg, rd, "sim-1", snap["id"])    # 进程退出之后照常回滚
    assert result["archive_path"] and _count(db, "engrams") == 0


def _migrated_world_db_at(reg: dict, name: str) -> str:
    db = reg["worlds"][name]["db_path"]
    adapter = _core.SqliteAdapter.open(db)   # 跑迁移建库
    del adapter
    gc.collect()
    return db


def test_rollback_active_world_in_use_reopens_the_untouched_db(tmp_path, monkeypatch):
    """活动世界：C++ 的 world_in_use 与其他 C++ 错误一样，按原 config 重建引擎、原样抛出。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    snap = _write_snapshot_write(mgr, state, "default")
    db, cfg = state.config.db_path, state.config
    _checkpoint(db)
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)    # 不在租约里的连接，读过库
    try:
        assert reader.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 2
        old_rt = weakref.ref(state.engine._rt)
        inode = os.stat(db).st_ino
        out, err = _rollback_outcome(mgr, "default", snap["id"])
        assert os.stat(db).st_ino == inode
        assert out is None and isinstance(err, _core.WorldStoreError)
        assert _code(err) == "world_in_use"
        assert old_rt() is None and state.engine is not None and state.engine._cfg is cfg
        assert mgr.switching is False and mgr.archives("default") == []
        _remember(state.engine, "Dave owns search")              # 重建出来的引擎能写
        assert _count(db) == 3
    finally:
        reader.close()
    out = mgr.rollback("default", snap["id"])
    assert out["active"] is True and _count(db) == 1


# ── B4：替换成功、重开失败单独成码，路由仍广播 ──

def _recording_app(tmp_path):
    app = create_app(_cfg(tmp_path))
    app.state.engine = DashboardEngine(app.state.config)
    sent = []

    async def _record(message):
        sent.append(message)
    app.state.ws_manager.broadcast = _record
    return TestClient(app), app, sent


def test_routes_rollback_reopen_failure_after_replace_is_500_and_broadcasts(tmp_path,
                                                                           monkeypatch):
    import starling.dashboard.world_manager as wm
    client, app, sent = _recording_app(tmp_path)
    _remember(app.state.engine, "Bob owns auth")
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    _remember(app.state.engine, "Carol owns billing")
    snap_path = app.state.worlds.load()["snapshots"]["default"][0]["path"]
    sent.clear()                                                  # snapshot_created

    def _fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _fail)
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (500, "rollback_reopen_failed")
    assert sent == [{"type": "world_rolled_back",
                     "payload": {"world": "default", "snapshot_id": snap["id"]}}]
    db = app.state.config.db_path
    with open(db, "rb") as live, open(snap_path, "rb") as frozen:
        assert live.read() == frozen.read()                       # 库就是快照的内容
    assert app.state.engine is None and _count(db) == 1


def test_routes_rollback_reopen_failure_after_cpp_error_is_world_open_failed(tmp_path,
                                                                            monkeypatch):
    import starling.dashboard.world_manager as wm
    client, app, sent = _recording_app(tmp_path)
    _remember(app.state.engine, "Bob owns auth")
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    _remember(app.state.engine, "Carol owns billing")
    with open(app.state.worlds.load()["snapshots"]["default"][0]["path"], "wb") as fh:
        fh.write(b"not sqlite" * 512)
    db = app.state.config.db_path
    inode = os.stat(db).st_ino
    sent.clear()

    def _fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _fail)
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (500, "world_open_failed")
    assert sent == []
    assert os.stat(db).st_ino == inode and _count(db) == 2


# ───────────────────────── W3 Codex 修复轮 ─────────────────────────

# ── C1：停引擎之前的回滚预检 ──

def test_binding_check_rollback_codes_and_touches_nothing(tmp_path):
    rd = str(tmp_path)
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, rd, "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    db = _migrated_world_db_at(reg, "sim-1")
    out = _core.world_take_snapshot(reg, "sim-1")
    reg, snap = out["registry"], out["snapshot"]
    _insert_foreign_engram(db, "sim-1")
    with open(db, "rb") as fh:
        before = fh.read()
    listing = sorted(os.listdir(os.path.dirname(db)))
    assert _core.world_check_rollback(reg, rd, "sim-1", snap["id"]) is None   # 有效快照：通过
    for call, code in (
            (lambda: _core.world_check_rollback(reg, rd, "nope", snap["id"]), "world_not_found"),
            (lambda: _core.world_check_rollback(reg, rd, "sim-1", "nope"), "snapshot_not_found"),
            (lambda: _core.world_check_rollback(reg, rd, "sim-1", snap["id"],
                                                vector_backend="zvec"),
             "snapshot_unsupported_backend")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _code(ei.value) == code
    os.remove(snap["path"])
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_check_rollback(reg, rd, "sim-1", snap["id"])
    assert _code(ei.value) == "snapshot_corrupt"
    with open(db, "rb") as fh:
        assert fh.read() == before
    assert sorted(os.listdir(os.path.dirname(db))) == listing      # 没建 archive/、没留 partial
    assert _core.world_list_archives(reg, rd, "sim-1") == []


@pytest.mark.parametrize("case,status,code", [
    ("unknown_snapshot_id", 404, "snapshot_not_found"),
    ("missing_snapshot_file", 409, "snapshot_corrupt"),
])
def test_routes_rollback_preflight_refusal_keeps_the_live_engine(tmp_path, monkeypatch,
                                                                 case, status, code):
    """活动世界上，不改输入重试也过不了的请求（不存在的快照 id、快照文件不在）在停引擎之前就被
    C++ 预检拒掉：同一个引擎对象、后台线程从没被停、切换窗口从没打开、库文件 inode 不变。
    没有预检时这类请求会触发一整轮停线程、丢引擎、重建（Codex W3 review Important 1）。"""
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr, eng = app.state.worlds, app.state.engine
    _remember(eng, "Bob owns auth")
    snap = mgr.take_snapshot("default", "")
    _remember(eng, "Carol owns billing")
    snapshot_id = snap["id"]
    if case == "unknown_snapshot_id":
        snapshot_id = "20990101T000000Z-dead"
    else:
        os.remove(snap["path"])
    db = app.state.config.db_path
    inode = os.stat(db).st_ino
    quiesced, windows = [], []
    real_quiesce, real_window = eng.quiesce, mgr._switch_window

    def _spy_quiesce(*args, **kwargs):
        quiesced.append(True)
        return real_quiesce(*args, **kwargs)
    monkeypatch.setattr(eng, "quiesce", _spy_quiesce)

    def _spy_window():
        windows.append(mgr.switching)
        return real_window()
    monkeypatch.setattr(mgr, "_switch_window", _spy_window)

    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snapshot_id))
    # 先断言引擎没被拆：回退验证时失败信息指向「引擎被拆了」，而不是状态码。
    assert app.state.engine is eng, "预检失败却拆掉了引擎"
    assert quiesced == [], "预检失败却停了后台线程"
    assert windows == [] and mgr.switching is False
    assert os.stat(db).st_ino == inode and _count(db) == 2
    assert (r.status_code, r.json()["detail"]) == (status, code)
    assert mgr.archives("default") == []
    monkeypatch.undo()
    _remember(app.state.engine, "Dave owns search")               # 引擎照常可用
    assert _count(db) == 3


# ───────────────────────── 加固 T2：失败路径丢掉引擎后，惰性重建补起后台线程 ─────────────────────────

def _lazy_build(cfg):
    """命令路由惰性建引擎用的那个真实构建（routes/commands.py 的 _build_engine）。"""
    from starling.dashboard.routes.commands import _build_engine
    return _build_engine(cfg)


def _drop_engine_by_failure(mgr, state, monkeypatch, failure: str) -> str:
    """走一条失败路径把引擎丢成 None，返回错误码。只在失败期间换掉 world_manager 的
    DashboardEngine，返回前换回来。
    - rollback_reopen：回滚替换成功、重开失败（rollback_reopen_failed）；
    - rollback_cpp_error：快照损坏、C++ 拒绝、重开也失败（world_open_failed）；
    - switch_double：切换时新世界打不开、回到旧世界也打不开（world_open_failed）。"""
    import starling.dashboard.world_manager as wm
    real = wm.DashboardEngine
    if failure == "switch_double":
        mgr.create("sim-1", "")

        def _call():
            mgr.activate("sim-1")
    else:
        snap = _write_snapshot_write(mgr, state, "default")
        if failure == "rollback_cpp_error":
            with open(snap["path"], "wb") as fh:
                fh.write(b"not sqlite" * 512)

        def _call():
            mgr.rollback("default", snap["id"])

    def _fail(_cfg):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(wm, "DashboardEngine", _fail)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            _call()
    finally:
        monkeypatch.setattr(wm, "DashboardEngine", real)
    assert state.engine is None and mgr.switching is False
    return ei.value.code


def _start_workers_like_lifespan(mgr, state, tick: float) -> None:
    state.config.tick_interval_s = tick
    mgr.workers_enabled = True
    mgr._start_workers(state.engine, state.config)


@pytest.mark.parametrize("tick", [0, 60.0])
@pytest.mark.parametrize("failure, code", [
    ("rollback_reopen", "rollback_reopen_failed"),
    ("rollback_cpp_error", "world_open_failed"),
    ("switch_double", "world_open_failed"),
])
def test_lazy_rebuild_after_a_failure_restarts_background_workers(tmp_path, monkeypatch,
                                                                  failure, code, tick):
    """运行中的引擎因失败路径被丢成 None 之后，下一次惰性重建出来的引擎要把摄入线程（tick>0 时
    还有维护 tick）起起来，否则 spool 不再被消费、tick 停摆，直到重启服务。只起一次：之后拿到
    已有引擎的租约不再起。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _start_workers_like_lifespan(mgr, state, tick)
    assert _drop_engine_by_failure(mgr, state, monkeypatch, failure) == code
    starts = []
    real_start = mgr._start_workers

    def _spy_start(engine, cfg):
        starts.append(engine)
        real_start(engine, cfg)
    monkeypatch.setattr(mgr, "_start_workers", _spy_start)
    lease = mgr.lease(build=_lazy_build)
    eng = lease.engine
    try:
        assert eng is not None and state.engine is eng and eng._cfg is state.config
        assert state.config.tenant == "default"
        assert eng._ingest_thread is not None and eng._ingest_thread.is_alive(), \
            "失败之后惰性重建的引擎没有起摄入线程"
        if tick:
            assert eng._tick_thread is not None and eng._tick_thread.is_alive()
        else:
            assert eng._tick_thread is None              # tick_interval_s=0：照旧不起 tick
        assert starts == [eng] and mgr._workers_lost is False
        threads = (eng._tick_thread, eng._ingest_thread)
        with mgr.lease(build=_lazy_build) as again:      # 已有引擎：不碰标志，不再起线程
            assert again is eng
        assert starts == [eng] and (eng._tick_thread, eng._ingest_thread) == threads
    finally:
        lease.release()
        assert eng is None or eng.quiesce() is True


@pytest.mark.parametrize("failure", ["rollback_reopen", "switch_double"])
def test_lazy_rebuild_after_a_failure_starts_no_workers_without_lifespan(tmp_path, monkeypatch,
                                                                        failure):
    """没有 lifespan（workers_enabled 为 False，例如直接构造的 TestClient(app)）：失败之后的惰性
    重建同样不起线程，与切换、回滚的既有行为一致。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    assert mgr.workers_enabled is False
    _drop_engine_by_failure(mgr, state, monkeypatch, failure)
    with mgr.lease(build=_lazy_build) as eng:
        assert eng is not None and state.engine is eng
        assert eng._tick_thread is None and eng._ingest_thread is None
    assert mgr._workers_lost is False


def test_lazy_rebuild_after_disable_workers_starts_no_workers(tmp_path, monkeypatch):
    """关停屏障关掉开关之后，惰性重建不起线程（服务正在关停）。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _start_workers_like_lifespan(mgr, state, 60.0)
    _drop_engine_by_failure(mgr, state, monkeypatch, "rollback_reopen")
    mgr.disable_workers()
    assert mgr.workers_enabled is False
    with mgr.lease(build=_lazy_build) as eng:
        assert eng is not None and state.engine is eng
        assert eng._tick_thread is None and eng._ingest_thread is None


class _ObservedLock:
    """包住一把 Lock：有人进 with 时先 set 事件再去拿锁，用来确定性地看出「有人在等这把锁」。"""

    def __init__(self, inner, entering: threading.Event) -> None:
        self._inner, self._entering = inner, entering

    def __enter__(self):
        self._entering.set()
        self._inner.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self._inner.release()


def test_shutdown_waits_for_an_in_flight_lazy_rebuild_before_disabling_workers(tmp_path,
                                                                              monkeypatch):
    """关停屏障（真实 lifespan）：惰性重建已建好引擎、已过 workers_enabled 检查、正要起线程（引擎
    在线程起好之后才写回）时，lifespan 开始关停。关停若不等 _build_lock，就会先关开关、按
    app.state.engine 停线程（此刻还是 None，或者还一条线程都没有），随后重建才把线程起来并写回，
    服务关停之后线程还在跑。等了 _build_lock，关停就排在重建之后，停掉的正是重建刚起的线程。卡点
    用 Event，不靠睡眠。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    cfg = _cfg(tmp_path)
    cfg.tick_interval_s = 60.0
    app = create_app(cfg)
    mgr = app.state.worlds
    client = TestClient(app)
    client.__enter__()                                 # lifespan：建引擎、起线程、打开开关
    stalled, proceed, shut = threading.Event(), threading.Event(), threading.Event()
    progress = threading.Event()                       # 关停走完，或关停开始拿 _build_lock
    real_build_lock = mgr._build_lock
    built: list = []
    lazy = stopper = None
    try:
        assert mgr.workers_enabled is True
        _drop_engine_by_failure(mgr, app.state, monkeypatch, "rollback_reopen")

        def _build_then_stall(build_cfg):
            eng = _lazy_build(build_cfg)
            real_tick = eng.start_background_tick

            def _stalled_tick(*args, **kwargs):
                stalled.set()                          # 已过 workers_enabled 检查，线程还没起
                assert proceed.wait(15)
                real_tick(*args, **kwargs)
            eng.start_background_tick = _stalled_tick
            built.append(eng)
            return eng

        def _lazy_request():
            mgr.lease(build=_build_then_stall).release()

        lazy = threading.Thread(target=_lazy_request, daemon=True)
        lazy.start()
        assert stalled.wait(10)

        def _shutdown():
            client.__exit__(None, None, None)          # 走 lifespan 的关停段
            shut.set()
            progress.set()

        # 重建此刻持着真正的 _build_lock（with 语句留着它自己的引用）；换上一个会报信的包装，
        # 关停一来拿它就知道。于是「关停已走完」与「关停卡在 _build_lock 上」两种结局都有确定的
        # 信号，等其一即可，不靠睡眠猜时序。
        mgr._build_lock = _ObservedLock(real_build_lock, progress)
        stopper = threading.Thread(target=_shutdown, daemon=True)
        stopper.start()
        assert progress.wait(10), "关停既没走完，也没去拿 _build_lock"
        shut_before_rebuild = shut.is_set()
        proceed.set()
        lazy.join(15)
        stopper.join(15)
        assert not lazy.is_alive() and not stopper.is_alive()
        eng = built[0]
        assert app.state.engine is eng
        assert eng._tick_thread is None and eng._ingest_thread is None, \
            "服务关停之后，惰性重建才起的后台线程还在跑"
        assert not shut_before_rebuild
        assert mgr.workers_enabled is False
    finally:
        proceed.set()
        if stopper is None:
            client.__exit__(None, None, None)
        for thread in (lazy, stopper):
            if thread is not None:
                thread.join(15)
        mgr._build_lock = real_build_lock
        for eng in built:
            vars(eng).pop("start_background_tick", None)
            eng.quiesce()


def test_worker_start_failure_keeps_the_lease_and_retries_on_the_next_real_rebuild(
        tmp_path, monkeypatch):
    """补起线程失败（例如 start_ingest_worker 抛错）不能让这次租约失败：logger.exception 记下，
    标志保持置位。拿到已有引擎的租约不重试；下一次真正新建引擎时再起。

    日志用替身接住而不用 caplog：caplog 与 pytest 的报告 handler 会留着 LogRecord，它的回溯
    经 _restart_lost_workers 的帧（f_back）钉住 lease 的帧和其中的引擎，测试就验证不了「丢掉
    这个引擎后旧 Runtime 能回收」。生产的 handler 不留记录。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _start_workers_like_lifespan(mgr, state, 60.0)
    _drop_engine_by_failure(mgr, state, monkeypatch, "rollback_reopen")
    logged = []

    def _spy_exception(msg, *args, **_kwargs):
        logged.append((msg % args, repr(sys.exc_info()[1])))   # 只留字符串，不留回溯
    monkeypatch.setattr(wm.logger, "exception", _spy_exception)

    def _build_with_broken_ingest(cfg):
        eng = _lazy_build(cfg)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("spool unreadable")
        eng.start_ingest_worker = _boom
        return eng

    lease = mgr.lease(build=_build_with_broken_ingest)
    first = lease.engine
    try:
        assert first is not None and state.engine is first
        assert first._ingest_thread is None
        assert mgr._workers_lost is True
        assert len(logged) == 1 and "spool unreadable" in logged[0][1]
        with mgr.lease(build=_lazy_build) as again:      # 已有引擎：不重试，标志不动
            assert again is first
        assert first._ingest_thread is None and mgr._workers_lost is True
        assert len(logged) == 1
    finally:
        lease.release()
    # 再丢一次引擎（停线程、丢引用、确认旧 Runtime 已回收：租约与管理器都没钉住它），下一次
    # 真正新建引擎时补起线程。
    assert first.quiesce() is True                    # 起线程中途失败：tick 线程已起，停掉
    rt_ref = weakref.ref(first._rt)
    state.engine = first = again = None
    gc.collect()
    assert rt_ref() is None, "租约或管理器钉住了补起线程失败的那个引擎"
    with mgr.lease(build=_lazy_build) as eng:
        assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
        assert eng._tick_thread is not None and eng._tick_thread.is_alive()
    assert mgr._workers_lost is False and len(logged) == 1
    assert eng.quiesce() is True


@pytest.mark.parametrize("failure", ["rollback_reopen", "rollback_cpp_error", "switch_double"])
@pytest.mark.parametrize("recovery", ["activate", "rollback", "activate_fails_back"])
def test_failure_sets_the_lost_workers_flag_and_a_successful_publish_clears_it(
        tmp_path, monkeypatch, failure, recovery):
    """两条失败路径把引擎丢成 None 时置位；之后成功的切换或回滚发布新引擎时已经起过线程，
    清位，免得之后某次惰性重建再起一遍。activate_fails_back：切到一个打不开的世界、回到旧世界
    成功（_rollback 的单失败恢复），旧世界的引擎同样起过线程，同样清位。

    清位之后正常丢掉引擎（不是失败路径）再惰性重建，不会再起一套线程：标志残留的话就会起。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    assert mgr._workers_lost is False
    _start_workers_like_lifespan(mgr, state, 60.0)
    _drop_engine_by_failure(mgr, state, monkeypatch, failure)
    assert mgr._workers_lost is True
    if recovery == "activate":
        if "sim-1" not in mgr.load()["worlds"]:
            mgr.create("sim-1", "")
        mgr.activate("sim-1")
    elif recovery == "rollback":
        snap = mgr.take_snapshot("default", "")
        mgr.rollback("default", snap["id"])
    else:
        bad_db = mgr.create("bad", "")["db_path"]
        real = wm.DashboardEngine

        def _bad_world_will_not_open(cfg):
            if cfg.db_path == bad_db:
                raise RuntimeError("bad world will not open")
            return real(cfg)
        monkeypatch.setattr(wm, "DashboardEngine", _bad_world_will_not_open)
        try:
            with pytest.raises(WorldSwitchError) as ei:
                mgr.activate("bad")
        finally:
            monkeypatch.setattr(wm, "DashboardEngine", real)
        assert ei.value.code == "world_open_failed"
        assert "also failed" not in ei.value.detail          # 单失败：旧世界重开成功
        assert state.config.tenant == "default" and mgr.load()["active"] == "default"
        del ei
    eng = state.engine
    try:
        assert eng is not None and mgr._workers_lost is False
        assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
    finally:
        assert eng is None or eng.quiesce() is True
    rt_ref = weakref.ref(eng._rt)
    state.engine = eng = None
    gc.collect()
    assert rt_ref() is None
    try:
        with mgr.lease(build=_lazy_build) as rebuilt:
            assert rebuilt is not None and state.engine is rebuilt
            assert rebuilt._tick_thread is None and rebuilt._ingest_thread is None, \
                "标志没清：之后的惰性重建又起了一套后台线程"
    finally:
        assert state.engine is None or state.engine.quiesce() is True


@pytest.mark.parametrize("op", ["activate", "rollback"])
def test_switch_stops_the_workers_an_in_flight_lazy_rebuild_started_before_taking_it_over(
        tmp_path, monkeypatch, op):
    """失败路径丢过引擎（_workers_lost）之后，惰性重建正在进行时，切换或活动世界回滚开始：
    _quiesce_and_drain 第一次读到的引擎还是 None，什么线程都停不到；等租约期间重建写回引擎、
    补起 tick 与摄入线程，然后归还租约，切换随后把这个引擎当旧引擎接手。等完租约之后不再停一次
    的话，它的线程一直在跑、_loop 闭包钉住引擎，旧 Runtime 回收不了：activate 把它记进
    _leaked_runtimes，线程活过关停；rollback 回 ingest_busy，并在同一个库上再开一个引擎，
    两个写者、两个摄入 worker。

    卡点全用 Event：build 卡住时租约已计数（切换必须等它）；_drain_leases 一被调用，就说明
    切换已置位、第一次 quiesce 已经读过 None，这时才放行构建。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _start_workers_like_lifespan(mgr, state, 60.0)
    _drop_engine_by_failure(mgr, state, monkeypatch, "rollback_reopen")
    assert state.engine is None and mgr._workers_lost is True and mgr.workers_enabled is True
    if op == "activate":
        mgr.create("sim-1", "")

        def _operate():
            return mgr.activate("sim-1")
    else:
        snap = mgr.take_snapshot("default", "")

        def _operate():
            return mgr.rollback("default", snap["id"])
    ingest_before = {t for t in threading.enumerate() if t.name == "starling-ingest"}
    in_build, proceed, draining = threading.Event(), threading.Event(), threading.Event()
    # 每次 _start_workers 之后记一笔：(Runtime 弱引用, 两个线程, 两个停止事件)。不留引擎本身，
    # 否则测试自己就钉住了被接手的引擎。线程跑完后 Thread 会丢掉 _target，不再引用引擎。
    started: list = []
    real_start = mgr._start_workers

    def _spy_start(engine, cfg):
        real_start(engine, cfg)
        started.append((weakref.ref(engine._rt),
                        (engine._tick_thread, engine._ingest_thread),
                        (engine._tick_stop, engine._ingest_stop)))
    monkeypatch.setattr(mgr, "_start_workers", _spy_start)
    real_drain = mgr._drain_leases

    def _signal_drain():
        draining.set()
        real_drain()
    monkeypatch.setattr(mgr, "_drain_leases", _signal_drain)

    def _blocked_build(cfg):
        in_build.set()
        assert proceed.wait(15)
        return _lazy_build(cfg)

    errors: list = []                       # 只留 repr，不留回溯（回溯会钉住帧里的引擎）
    outcome: list = []

    def _lazy_request():
        try:
            mgr.lease(build=_blocked_build).release()
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"lease: {exc!r}")

    def _switch():
        try:
            outcome.append(_operate())
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"{op}: {exc!r}")

    lazy = threading.Thread(target=_lazy_request, daemon=True)
    switcher = threading.Thread(target=_switch, daemon=True)
    try:
        lazy.start()
        assert in_build.wait(10)
        switcher.start()
        assert draining.wait(10), "切换没有走到等租约这一步"
        proceed.set()
        lazy.join(15)
        switcher.join(30)
        assert not lazy.is_alive() and not switcher.is_alive()
        assert errors == []
        assert len(outcome) == 1 and len(started) == 2      # 惰性重建一次 + 切换/回滚发布一次
        lazy_rt, lazy_threads, _stops = started[0]
        assert all(t is not None for t in lazy_threads), "惰性重建没补起线程：没走到要测的交错"
        assert not any(t.is_alive() for t in lazy_threads), "被接手的惰性引擎的后台线程还在跑"
        gc.collect()
        assert lazy_rt() is None, "被接手的惰性引擎的 Runtime 没被回收"
        assert mgr._leaked_runtimes == []
        final = state.engine
        assert final is not None and final._ingest_thread is not None
        live = [t for t in threading.enumerate()
                if t.name == "starling-ingest" and t.is_alive() and t not in ingest_before]
        assert live == [final._ingest_thread], "同一进程里不止一个摄入 worker"
        if op == "rollback":
            assert outcome[0]["active"] is True
        else:
            assert state.config.tenant == "sim-1"
        del final
    finally:
        proceed.set()
        for thread in (lazy, switcher):
            if thread.ident is not None:
                thread.join(15)
        for _rt, threads, stops in started:             # 回退验证时这些线程还活着：停掉
            for stop in stops:
                if stop is not None:
                    stop.set()
            for thread in threads:
                if thread is not None:
                    thread.join(15)
        if state.engine is not None:
            state.engine.quiesce()


@pytest.mark.parametrize("op", ["activate", "rollback"])
def test_switch_refuses_when_the_taken_over_lazy_engine_will_not_stop(tmp_path, monkeypatch, op):
    """等完租约之后的第二次 quiesce 停不下（线程卡住）：与第一次同样处理，恢复这个引擎的后台线程、
    引擎留在 app.state 上，回 ingest_busy，detail 点名卡住的线程，状态不前进。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    state.config.tick_interval_s = 60.0
    mgr.workers_enabled = True
    cfg = state.config
    late = state.engine
    state.engine = None                       # 第一次 quiesce 读到 None
    if op == "activate":
        mgr.create("sim-1", "")

        def _operate():
            mgr.activate("sim-1")
    else:
        snap = mgr.take_snapshot("default", "")

        def _operate():
            mgr.rollback("default", snap["id"])
    late._tick_thread, late._tick_stop = _StuckThread(), threading.Event()
    real_drain = mgr._drain_leases

    def _drain_then_write_back():
        real_drain()
        state.engine = late                   # 在途惰性构建在等租约期间写回、起了线程
    monkeypatch.setattr(mgr, "_drain_leases", _drain_then_write_back)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            _operate()
        assert ei.value.code == "ingest_busy"
        assert "tick" in ei.value.detail and "ingest" not in ei.value.detail
        del ei
        assert state.engine is late and state.config is cfg
        assert mgr.load()["active"] == "default" and mgr.switching is False
        assert late._ingest_thread is not None and late._ingest_thread.is_alive()   # 已恢复
    finally:
        late._tick_thread = late._tick_stop = None
        assert late.quiesce() is True


def test_startup_without_an_engine_still_starts_no_ingest_worker_on_first_lazy_build(
        tmp_path, monkeypatch):
    """启动语义不变：tick_interval_s=0、启动时没有引擎，lifespan 不建引擎也不起摄入线程；第一条
    命令请求惰性建出的引擎同样不起（没有失败路径丢过引擎，标志从未置位）。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        assert app.state.engine is None and app.state.worlds.workers_enabled is True
        assert client.post("/api/tick", json={}).status_code == 200
        eng = app.state.engine
        assert eng is not None and app.state.worlds._workers_lost is False
        assert eng._tick_thread is None and eng._ingest_thread is None


# ───────────────────────── 整支终审修复轮 I1：惰性重建正在起线程时开始切换 ─────────────────────────

def _block_first_start_of(monkeypatch, name: str):
    """把名为 name 的线程的第一次 Thread.start() 卡住，直到返回的 release 被 set；卡住时 set
    in_start。只拦这一个名字的第一次启动，其余线程（包括测试自己的线程）照常启动。"""
    in_start, release = threading.Event(), threading.Event()
    real_start = threading.Thread.start

    def _blocking_start(thread):
        if thread.name == name and not in_start.is_set():
            in_start.set()
            release.wait(15)
        real_start(thread)
    monkeypatch.setattr(threading.Thread, "start", _blocking_start)
    return in_start, release


@pytest.mark.parametrize("blocked", ["starling-bg-tick", "starling-ingest"])
@pytest.mark.parametrize("op", ["activate", "rollback"])
def test_switch_during_a_lazy_rebuild_that_is_still_starting_its_workers(tmp_path, monkeypatch,
                                                                         op, blocked):
    """失败路径丢过引擎之后，惰性重建正卡在补起线程的 Thread.start() 里（还没返回），切换或活动
    世界回滚开始。lease 若先写回引擎、后起线程，切换的第一次 quiesce 就会读到这个引擎；engine
    若先挂句柄、后 start，join 一个没启动的线程抛 RuntimeError，路由回 500 internal_error，那条
    线程随后启动、看到停止事件就退出，句柄却留着，标志也已清掉，摄入或 tick 静默停摆。

    现在 lease 先起线程、后写回：切换的第一次 quiesce 读到的只能是 None，它去等租约，惰性重建
    起完线程、写回、归还租约之后，第二次 quiesce 停掉它的线程，切换照常完成。卡点全用 Event。"""
    monkeypatch.delenv("STARLING_WORLDS_REGISTRY", raising=False)
    mgr, state = _manager(tmp_path)
    _start_workers_like_lifespan(mgr, state, 60.0)
    _drop_engine_by_failure(mgr, state, monkeypatch, "rollback_reopen")
    assert state.engine is None and mgr._workers_lost is True and mgr.workers_enabled is True
    if op == "activate":
        mgr.create("sim-1", "")

        def _operate():
            return mgr.activate("sim-1")
    else:
        snap = mgr.take_snapshot("default", "")

        def _operate():
            return mgr.rollback("default", snap["id"])
    ingest_before = {t for t in threading.enumerate() if t.name == "starling-ingest"}
    in_start, release = _block_first_start_of(monkeypatch, blocked)
    progress = threading.Event()            # 切换走到了等租约这一步，或者已经结束
    # 每次 _start_workers 之后记 (Runtime 弱引用, 两个线程, 两个停止事件)，不留引擎本身。
    started: list = []
    real_start_workers = mgr._start_workers

    def _spy_start(engine, cfg):
        real_start_workers(engine, cfg)
        started.append((weakref.ref(engine._rt),
                        (engine._tick_thread, engine._ingest_thread),
                        (engine._tick_stop, engine._ingest_stop)))
    monkeypatch.setattr(mgr, "_start_workers", _spy_start)
    published_at_drain: list = []           # 等租约时 app.state.engine 是否已经写回（只留布尔）
    real_drain = mgr._drain_leases

    def _signal_drain():
        published_at_drain.append(state.engine is not None)
        progress.set()
        real_drain()
    monkeypatch.setattr(mgr, "_drain_leases", _signal_drain)

    errors: list = []                       # 只留 repr，不留回溯（回溯会钉住帧里的引擎）
    outcome: list = []

    def _lazy_request():
        try:
            mgr.lease(build=_lazy_build).release()
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"lease: {exc!r}")

    def _switch():
        try:
            outcome.append(_operate())
        except WorldSwitchError as exc:
            errors.append(f"{op}: WorldSwitchError({exc.code!r})")
        except BaseException as exc:  # noqa: BLE001 — 路由会把它兜成 500 internal_error
            errors.append(f"{op}: raw {exc!r}")
        finally:
            progress.set()

    lazy = threading.Thread(target=_lazy_request, daemon=True)
    switcher = threading.Thread(target=_switch, daemon=True)
    try:
        lazy.start()
        assert in_start.wait(10), "惰性重建没有走到起线程这一步"
        switcher.start()
        assert progress.wait(10), "切换既没走到等租约，也没结束"
        release.set()
        lazy.join(15)
        switcher.join(30)
        assert not lazy.is_alive() and not switcher.is_alive()
        assert [e for e in errors if " raw " in e] == [], "切换抛了裸异常（路由回 500 internal_error）"
        assert errors == []
        assert published_at_drain == [False], "切换的第一次 quiesce 读到了还在起线程的惰性引擎"
        assert len(outcome) == 1 and len(started) == 2      # 惰性重建一次 + 切换/回滚发布一次
        lazy_rt, lazy_threads, _stops = started[0]
        assert all(t is not None for t in lazy_threads), "惰性重建没补起线程：没走到要测的交错"
        assert not any(t.is_alive() for t in lazy_threads), "被接手的惰性引擎的后台线程还在跑"
        gc.collect()
        assert lazy_rt() is None, "被接手的惰性引擎的 Runtime 没被回收"
        assert mgr._leaked_runtimes == [] and mgr._workers_lost is False
        final = state.engine
        assert final is not None
        assert final._tick_thread is not None and final._tick_thread.is_alive()
        assert final._ingest_thread is not None and final._ingest_thread.is_alive()
        live = [t for t in threading.enumerate()
                if t.name == "starling-ingest" and t.is_alive() and t not in ingest_before]
        assert live == [final._ingest_thread], "同一进程里不止一个摄入 worker"
        if op == "rollback":
            assert outcome[0]["active"] is True
        else:
            assert state.config.tenant == "sim-1"
        del final
    finally:
        release.set()
        for thread in (lazy, switcher):
            if thread.ident is not None:
                thread.join(15)
        for _rt, threads, stops in started:             # 回退验证时这些线程可能还活着：停掉
            for stop in stops:
                if stop is not None:
                    stop.set()
            for thread in threads:
                if thread is not None:
                    thread.join(15)
        if state.engine is not None:
            state.engine.quiesce()


@pytest.mark.parametrize("blocked", ["starling-bg-tick", "starling-ingest"])
def test_worker_handle_is_set_only_after_the_thread_has_started(tmp_path, monkeypatch, blocked):
    """纵深防御（引擎层）：start_background_tick / start_ingest_worker 在 Thread.start() 返回之后才
    挂句柄。卡在 start() 里的这段时间，quiesce 看不到句柄，不会去 join 一个没启动的线程（那会抛
    RuntimeError，并留下一个线程退出、句柄还在的引擎，此后 start_* 一律 no-op）；start() 返回后
    句柄挂上，线程在跑。"""
    eng = DashboardEngine(_cfg(tmp_path))
    in_start, release = _block_first_start_of(monkeypatch, blocked)

    def _start():
        if blocked == "starling-bg-tick":
            eng.start_background_tick(60.0)
        else:
            eng.start_ingest_worker(poll_interval_s=60.0)

    starter = threading.Thread(target=_start, daemon=True)
    try:
        starter.start()
        assert in_start.wait(10)
        assert eng.quiesce() is True              # 没有可见的句柄：什么都不 join，也不抛
        assert (eng._tick_thread, eng._ingest_thread) == (None, None)
        release.set()
        starter.join(15)
        assert not starter.is_alive()
        thread = eng._tick_thread if blocked == "starling-bg-tick" else eng._ingest_thread
        assert thread is not None and thread.is_alive()
        assert thread.name == blocked
        del thread
    finally:
        release.set()
        starter.join(15)
        eng.quiesce()


# ───────────────────────── W4 T2：自动快照（绑定、触发核心、会话钩子、调度线程） ─────────────────────────

_SECONDS_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class _Clock:
    """可推进的注入时钟：WorldManager 每次取时刻都调它。"""
    def __init__(self, now: str = "2026-10-28T00:00:00Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


def _auto_manager(tmp_path, clock=None):
    cfg = _cfg(tmp_path)
    reg_path = bootstrap_worlds(cfg)
    state = _State(cfg)
    state.engine = DashboardEngine(cfg)
    return WorldManager(state, reg_path, clock=clock), state


def _turns(session_id: str, observed: str = "2026-10-27T09:00:00Z") -> str:
    speakers = ["self", "claude-code:proj"]
    return json.dumps([
        {"speaker": speaker, "text": f"{session_id} turn {index}", "session_id": session_id,
         "turn_index": index, "audience": speakers, "provenance": "source_time",
         "turn_id": None, "observed_at": observed}
        for index, speaker in enumerate(speakers)])


def _retain_session(adapter, tenant: str, session_id: str) -> None:
    """真往 source_unit_meta 写一个会话的两条话轮（与摄入走同一个 C++ 写入口）。"""
    _core.retain_source_turns(adapter, tenant, ["self", "claude-code:proj"],
                              _turns(session_id), "2026-10-27T09:00:00Z")


def _hold_manager_lock(mgr):
    """另一个线程持住 manager.lock，直到返回的 Event 被 set。"""
    held, release = threading.Event(), threading.Event()

    def _holder():
        with mgr.lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(10)
    return holder, release


def _manager_lock_is_free(mgr) -> bool:
    """T2 review M-5：从另一个线程非阻塞取一次 manager.lock，取到就放开。RLock 可重入，在拿过锁的
    同一线程上反复取看不出泄漏，所以必须换线程看。"""
    def _probe() -> bool:
        if mgr.lock.acquire(blocking=False):
            mgr.lock.release()
            return True
        return False
    prober, result = _call_in_thread(_probe)
    assert not prober.is_alive()
    return result.get("value") is True


def _call_in_thread(fn, *args):
    """在独立线程里调 fn，最多等 5 秒。返回 (线程, 结果 dict)：结果里是 value 或 error。"""
    result: dict = {}

    def _run():
        try:
            result["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 — 测试要看见钩子抛了什么
            result["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(5)
    return worker, result


def _auto_snaps(mgr, world: str = "default") -> list:
    return [s for s in mgr.load()["snapshots"].get(world, []) if s["kind"] == "auto"]


def _scheduler_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "starling-auto-snapshot" and t.is_alive()]


class _Rounds:
    """把调度线程的每一轮变成测试可控的一步：线程每轮先等 step() 放行，再跑真的 _auto_snapshot，
    结果放进队列。close() 之后放行的轮次不再调真方法，stop_scheduler 才不会等一轮快照。"""

    def __init__(self, mgr, monkeypatch) -> None:
        self._go = threading.Semaphore(0)
        self._closed = False
        self.results: queue.Queue = queue.Queue()
        self.thread_names: set = set()
        real = mgr._auto_snapshot

        def _spy(kind, session_id="", **kwargs):
            # 先看 closed：close() 之后的轮次立刻返回，不再在信号量上等。
            if self._closed or not self._go.acquire(timeout=30) or self._closed:
                return "closed"
            self.thread_names.add(threading.current_thread().name)
            out = real(kind, session_id, **kwargs)
            self.results.put(out)
            return out

        monkeypatch.setattr(mgr, "_auto_snapshot", _spy)

    def step(self, timeout: float = 10.0) -> str:
        self._go.release()
        return self.results.get(timeout=timeout)

    def close(self) -> None:
        self._closed = True
        self._go.release()


# ── 绑定 ──

def test_binding_auto_snapshot_config_roundtrip(tmp_path):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    assert reg["worlds"]["default"]["auto_snapshot"] == {"on_session": False, "interval_hours": 24}
    plain = _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-28T00:00:00Z")
    assert plain["world"]["auto_snapshot"] == {"on_session": True, "interval_hours": 0}
    out = _core.world_create(plain["registry"], str(tmp_path), "sim-2", "", "2026-10-28T00:00:00Z",
                             auto_snapshot={"on_session": False, "interval_hours": 12})
    assert out["world"]["auto_snapshot"] == {"on_session": False, "interval_hours": 12}
    reg = out["registry"]
    assert reg["worlds"]["sim-2"]["auto_snapshot"] == {"on_session": False, "interval_hours": 12}

    updated = _core.world_set_auto_snapshot(reg, "sim-1", False, 6)
    assert updated["worlds"]["sim-1"]["auto_snapshot"] == {"on_session": False, "interval_hours": 6}
    assert reg["worlds"]["sim-1"]["auto_snapshot"] == {"on_session": True, "interval_hours": 0}

    reg_path = str(tmp_path / "w.json")
    _core.world_save_registry(reg_path, updated)
    assert _core.world_load_registry(reg_path)["worlds"]["sim-1"]["auto_snapshot"] == {
        "on_session": False, "interval_hours": 6}


@pytest.mark.parametrize("on_session, hours", [
    (True, 8761), (True, -1), (1, 6), (True, True), (True, 6.0), (True, "6"), (True, 2 ** 70)])
def test_binding_set_auto_snapshot_rejects_bad_config(tmp_path, on_session, hours):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_set_auto_snapshot(reg, "default", on_session, hours)
    assert _code(ei.value) == "invalid_auto_snapshot"
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-28T00:00:00Z",
                           auto_snapshot={"on_session": on_session, "interval_hours": hours})
    assert _code(ei.value) == "invalid_auto_snapshot"
    assert not os.path.exists(tmp_path / "worlds" / "sim-1")


@pytest.mark.parametrize("config", [{}, {"on_session": True}, {"interval_hours": 0}, [True, 0]])
def test_binding_create_rejects_malformed_auto_snapshot(tmp_path, config):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-28T00:00:00Z",
                           auto_snapshot=config)
    assert _code(ei.value) == "invalid_auto_snapshot"


def test_binding_set_auto_snapshot_unknown_world(tmp_path):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_set_auto_snapshot(reg, "nope", True, 0)
    assert _code(ei.value) == "world_not_found"


def test_binding_snapshot_dict_carries_w4_fields(tmp_path):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    db = reg["worlds"]["sim-1"]["db_path"]
    adapter = _core.SqliteAdapter.open(db)
    _retain_session(adapter, "sim-1", "s-old")

    manual = _core.world_take_snapshot(reg, "sim-1", note="手动")["snapshot"]
    assert manual["sessions_complete"] == 1
    assert manual["session_id"] is None
    assert isinstance(manual["content_mark"], str) and manual["content_mark"]

    out = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z", "s-new")
    snap = out["snapshot"]
    assert out["skipped"] is None and out["refused"] is None
    assert (snap["kind"], snap["reason"], snap["session_id"]) == ("auto", "session", "s-new")
    assert snap["created_at"] == "2026-10-28T08:00:00Z"
    assert snap["id"].startswith("20261028T080000Z-")
    assert snap["sessions_complete"] == 1 and snap["content_mark"] == manual["content_mark"]
    assert out["registry"]["snapshots"]["sim-1"][0]["id"] == snap["id"]
    assert reg.get("snapshots", {}).get("sim-1", []) == []          # 入参 dict 不变

    again = _core.world_take_auto_snapshot(out["registry"], "sim-1", "session",
                                           "2026-10-28T08:05:00Z", "s-new")
    assert again["snapshot"] is None and again["skipped"] == "unchanged"
    assert again["refused"] is None
    known = _core.world_take_auto_snapshot(out["registry"], "sim-1", "session",
                                           "2026-10-28T08:05:00Z", "s-old")
    assert known["snapshot"] is None and known["skipped"] == "known_session"
    zvec = _core.world_take_auto_snapshot(out["registry"], "sim-1", "session",
                                          "2026-10-28T08:05:00Z", "s-x", vector_backend="zvec")
    assert zvec["snapshot"] is None and zvec["skipped"] == "unsupported_backend"
    del adapter
    gc.collect()


def test_binding_take_auto_snapshot_rejects_microsecond_now(tmp_path):
    """C++ 只认秒精度（addendum M-5）：engine.py 里 isoformat 的那种时刻会被拒。"""
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_take_auto_snapshot(reg, "default", "interval", "2026-10-28T08:00:00.123456Z")
    assert _code(ei.value) == "invalid_auto_snapshot"


def test_binding_list_sessions_shape(tmp_path):
    reg = _core.world_ensure_registry(str(tmp_path / "w.json"), str(tmp_path / "d.db"), "default")
    reg = _core.world_create(reg, str(tmp_path), "sim-1", "", "2026-10-25T01:00:00Z")["registry"]
    assert _core.world_list_sessions(reg, "sim-1", 200) == {
        "total": 0, "null_observed": 0, "truncated": False, "sessions": []}

    db = reg["worlds"]["sim-1"]["db_path"]
    adapter = _core.SqliteAdapter.open(db)
    reg = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z",
                                         "s-1")["registry"]
    anchor = reg["snapshots"]["sim-1"][0]
    _retain_session(adapter, "sim-1", "s-1")
    _retain_session(adapter, "sim-1", "s-2")

    listed = _core.world_list_sessions(reg, "sim-1", 1)
    assert (listed["total"], listed["null_observed"], listed["truncated"]) == (2, 0, True)
    assert len(listed["sessions"]) == 1
    full = _core.world_list_sessions(reg, "sim-1", 200)
    rows = {row["session_id"]: row for row in full["sessions"]}
    assert set(rows) == {"s-1", "s-2"} and full["truncated"] is False
    assert rows["s-1"] == {"session_id": "s-1", "units": 2, "speakers": 2,
                           "first_at": "2026-10-27T09:00:00Z", "last_at": "2026-10-27T09:00:00Z",
                           "anchor": {"snapshot_id": anchor["id"],
                                      "created_at": "2026-10-28T08:00:00Z"}}
    assert rows["s-2"]["anchor"] is None
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_list_sessions(reg, "sim-1", 0)
    assert _code(ei.value) == "invalid_auto_snapshot"
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_list_sessions(reg, "nope", 10)
    assert _code(ei.value) == "world_not_found"
    del adapter
    gc.collect()


# ── WorldManager：配置与状态 ──

def test_manager_create_passes_auto_snapshot_config(tmp_path):
    mgr, _state = _auto_manager(tmp_path)
    assert mgr.create("sim-1", "")["auto_snapshot"] == {"on_session": True, "interval_hours": 0}
    entry = mgr.create("sim-2", "", auto_snapshot={"on_session": False, "interval_hours": 48})
    assert entry["auto_snapshot"] == {"on_session": False, "interval_hours": 48}
    assert mgr.load()["worlds"]["sim-2"]["auto_snapshot"] == {"on_session": False,
                                                              "interval_hours": 48}


def test_manager_set_auto_snapshot_persists_and_rejects(tmp_path):
    mgr, _state = _auto_manager(tmp_path)
    assert mgr.set_auto_snapshot("default", True, 6) == {"on_session": True, "interval_hours": 6}
    assert mgr.load()["worlds"]["default"]["auto_snapshot"] == {"on_session": True,
                                                                "interval_hours": 6}
    before = mgr.load()
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.set_auto_snapshot("default", True, 9000)
    assert _code(ei.value) == "invalid_auto_snapshot"
    with pytest.raises(_core.WorldStoreError) as ei:
        mgr.set_auto_snapshot("nope", True, 0)
    assert _code(ei.value) == "world_not_found"
    assert mgr.load() == before


def test_auto_snapshot_status_starts_clean(tmp_path):
    mgr, _state = _auto_manager(tmp_path)
    assert mgr.auto_snapshot_status() == {"failures": 0, "last_error": None, "last_error_at": None}


# ── _auto_snapshot：两种触发共用的核心 ──

def test_auto_snapshot_interval_follows_the_injected_clock(tmp_path):
    clock = _Clock("2026-10-28T00:00:00Z")
    mgr, state = _auto_manager(tmp_path, clock)
    _remember(state.engine, "Bob owns auth")

    first = mgr._auto_snapshot("interval")
    assert first.startswith("taken:"), first
    snaps = _auto_snaps(mgr)
    assert [s["id"] for s in snaps] == [first.split(":", 1)[1]]
    assert (snaps[0]["reason"], snaps[0]["created_at"]) == ("interval", "2026-10-28T00:00:00Z")
    assert snaps[0]["sessions_complete"] == 0
    assert os.path.isfile(snaps[0]["path"])

    clock.now = "2026-10-28T23:59:59Z"
    assert mgr._auto_snapshot("interval") == "skipped:not_due"
    clock.now = "2026-10-29T00:00:00Z"
    assert mgr._auto_snapshot("interval") == "skipped:unchanged"       # 到期了，但内容没变
    _remember(state.engine, "Carol owns billing")
    second = mgr._auto_snapshot("interval")
    assert second.startswith("taken:"), second
    assert [s["created_at"] for s in _auto_snaps(mgr)] == ["2026-10-29T00:00:00Z",
                                                           "2026-10-28T00:00:00Z"]
    assert mgr.auto_snapshot_status()["failures"] == 0


def test_auto_snapshot_default_clock_is_seconds_precision(tmp_path):
    """addendum M-5：不注入时钟时，交给 C++ 的必须是秒精度（YYYY-MM-DDTHH:MM:SSZ），否则每一轮
    都会 invalid_auto_snapshot、计为失败。"""
    mgr, state = _auto_manager(tmp_path)
    _remember(state.engine, "Bob owns auth")
    outcome = mgr._auto_snapshot("interval")
    assert outcome.startswith("taken:"), (outcome, mgr.auto_snapshot_status())
    assert _SECONDS_ISO.fullmatch(_auto_snaps(mgr)[0]["created_at"])
    assert mgr.auto_snapshot_status()["failures"] == 0


def test_auto_snapshot_registry_save_failure_discards_file_and_counts(tmp_path, monkeypatch):
    clock = _Clock("2026-10-28T00:00:00Z")
    mgr, state = _auto_manager(tmp_path, clock)
    _remember(state.engine, "Bob owns auth")
    before = mgr.load()
    snap_dir = before["worlds"]["default"]["snapshot_dir"]
    monkeypatch.setattr(mgr, "save", _boom_save)
    assert mgr._auto_snapshot("interval") == "failed"                  # 不往外抛
    monkeypatch.undo()
    assert mgr.load() == before
    leftovers = os.listdir(snap_dir) if os.path.isdir(snap_dir) else []
    assert leftovers == [], "注册表没存下来，新快照文件必须删掉"
    status = mgr.auto_snapshot_status()
    assert status == {"failures": 1, "last_error": "internal_error",
                      "last_error_at": "2026-10-28T00:00:00Z"}
    assert _manager_lock_is_free(mgr), "failed 之后 manager.lock 必须已放开"


def test_auto_snapshot_cpp_failure_is_counted_not_raised(tmp_path, monkeypatch, caplog):
    clock = _Clock("2026-10-28T00:00:00Z")
    mgr, state = _auto_manager(tmp_path, clock)
    _remember(state.engine, "Bob owns auth")

    def _no_space(*_args, **_kwargs):
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
    monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
    with caplog.at_level("WARNING", logger="starling.dashboard.world_manager"):
        assert mgr._auto_snapshot("interval") == "failed"
        clock.now = "2026-10-28T01:00:00Z"
        assert mgr._auto_snapshot("session", "s-1") == "failed"
    assert mgr.auto_snapshot_status() == {"failures": 2, "last_error": "insufficient_disk",
                                          "last_error_at": "2026-10-28T01:00:00Z"}
    assert "insufficient_disk" in caplog.text
    assert _auto_snaps(mgr) == []
    assert _manager_lock_is_free(mgr), "failed 之后 manager.lock 必须已放开"


def test_auto_snapshot_zvec_is_a_skip_not_a_failure(tmp_path):
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    state.config.vector_backend = "zvec"
    assert mgr._auto_snapshot("interval") == "skipped:unsupported_backend"
    assert mgr.auto_snapshot_status()["failures"] == 0
    assert _auto_snaps(mgr) == []


@pytest.mark.parametrize("moved", ["config", "engine"])
def test_auto_snapshot_skips_when_live_db_is_not_the_active_world(tmp_path, moved):
    """切换 / 回滚失败路径留下的分叉（注册表说 A，config 或引擎在写 B）：不给错的世界拍。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    elsewhere = str(tmp_path / "elsewhere.db")
    if moved == "config":
        state.config.db_path = elsewhere
    else:
        state.engine = DashboardEngine(DashboardConfig(
            db_path=elsewhere, token="", tick_interval_s=0, source_lane_enabled=False,
            ingest_spool_path=str(tmp_path / "spool")))
    assert mgr._auto_snapshot("interval") == "skipped:world_moved"
    assert _auto_snaps(mgr) == []
    assert mgr.auto_snapshot_status()["failures"] == 0
    assert _manager_lock_is_free(mgr), "world_moved 的提前 return 必须放开 manager.lock"


def test_auto_snapshot_is_busy_without_waiting_when_the_lock_is_held(tmp_path):
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    mgr.set_auto_snapshot("default", True, 24)
    hook = mgr._make_session_hook()
    holder, release = _hold_manager_lock(mgr)
    try:
        caller, result = _call_in_thread(mgr._auto_snapshot, "interval")
        assert not caller.is_alive(), "后台调用方在 manager.lock 上等了"
        assert result == {"value": "busy"}
        hooked, hook_result = _call_in_thread(hook, "s-1")
        assert not hooked.is_alive(), "会话钩子在 manager.lock 上等了"
        assert isinstance(hook_result.get("error"), SessionSnapshotDeferred), hook_result
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()
    assert _manager_lock_is_free(mgr), "busy 之后 manager.lock 必须已放开"
    assert _auto_snaps(mgr) == []
    assert mgr.auto_snapshot_status()["failures"] == 0          # busy 不是失败
    assert mgr._auto_snapshot("interval").startswith("taken:")  # 锁放开后照常
    assert _manager_lock_is_free(mgr)


# ── 会话钩子 ──

def test_session_hook_snapshots_a_new_session_once(tmp_path):
    clock = _Clock("2026-10-28T00:00:00Z")
    mgr, state = _auto_manager(tmp_path, clock)
    _remember(state.engine, "Bob owns auth")
    mgr.set_auto_snapshot("default", True, 0)
    hook = mgr._make_session_hook()

    assert hook("s-1") is None
    snaps = _auto_snaps(mgr)
    assert [(s["reason"], s["session_id"]) for s in snaps] == [("session", "s-1")]
    assert _count(snaps[0]["path"], "source_unit_meta") == 0     # 快照里还没有这个会话

    _retain_session(state.engine._rt.adapter, state.config.tenant, "s-1")
    clock.now = "2026-10-28T00:05:00Z"
    assert mgr._auto_snapshot("session", "s-1") == "skipped:known_session"
    assert hook("s-1") is None
    assert len(_auto_snaps(mgr)) == 1                            # 同一会话第二次不拍


def test_session_hook_retry_with_unchanged_content_is_a_normal_skip(tmp_path):
    """I-1：写入失败、会话一直是新的时，同一会话、同一标记不再拍；钩子照常返回。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    mgr.set_auto_snapshot("default", True, 0)
    hook = mgr._make_session_hook()
    assert hook("s-2") is None
    assert mgr._auto_snapshot("session", "s-2") == "skipped:unchanged"
    assert hook("s-2") is None
    assert len(_auto_snaps(mgr)) == 1


def test_session_hook_failure_returns_normally(tmp_path, monkeypatch):
    """用户裁定：快照失败照常摄入，钩子不抛。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    mgr.set_auto_snapshot("default", True, 0)

    def _no_space(*_args, **_kwargs):
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
    monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
    assert mgr._make_session_hook()("s-1") is None
    assert mgr.auto_snapshot_status()["failures"] == 1


@pytest.mark.parametrize("on_session", [False, True], ids=["on_session_off", "on_session_on"])
def test_session_hook_defers_only_when_the_active_world_snapshots_per_session(tmp_path,
                                                                             on_session):
    """T2 review M-3：按会话关着（默认世界的默认配置）时，钩子取锁之前就返回：manager.lock 被别的线程占着
    （手动快照、删除、清理归档、非活动世界的回滚、调度线程自己的快照）也不推迟摄入，不等锁，不拍。
    开着时照旧：取不到锁就抛 SessionSnapshotDeferred。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    mgr.set_auto_snapshot("default", on_session, 24)
    hook = mgr._make_session_hook()
    holder, release = _hold_manager_lock(mgr)
    try:
        hooked, result = _call_in_thread(hook, "s-1")
        assert not hooked.is_alive(), "会话钩子在 manager.lock 上等了"
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()
    if on_session:
        assert isinstance(result.get("error"), SessionSnapshotDeferred), result
    else:
        assert result == {"value": None}, result
    assert _auto_snaps(mgr) == []
    assert mgr.auto_snapshot_status()["failures"] == 0


def test_session_hook_falls_through_to_the_locked_path_when_the_lock_free_read_fails(
        tmp_path, monkeypatch):
    """M-3：锁外那次读注册表失败（registry_corrupt、瞬时读错）不算失败，也不当作关着跳过，而是走持锁
    路径：锁被占就照样推迟；锁空着就在锁内重新读、照常拍。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    mgr.set_auto_snapshot("default", True, 0)
    real_load = mgr.load
    failing = {"next": 0}

    def _load_failing_next():
        # 钩子先锁外读一次（提前退出），取到锁之后再读一次：只让前一次失败。
        if failing["next"] > 0:
            failing["next"] -= 1
            raise _core.WorldStoreError("registry_corrupt: transient read error")
        return real_load()
    monkeypatch.setattr(mgr, "load", _load_failing_next)
    hook = mgr._make_session_hook()

    failing["next"] = 1
    holder, release = _hold_manager_lock(mgr)
    try:
        hooked, result = _call_in_thread(hook, "s-1")
        assert not hooked.is_alive(), "会话钩子在 manager.lock 上等了"
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()
    assert failing["next"] == 0, "锁外那次读没有发生"
    assert isinstance(result.get("error"), SessionSnapshotDeferred), result     # 没跳过：照样推迟

    failing["next"] = 1
    assert hook("s-1") is None
    assert failing["next"] == 0
    assert [s["session_id"] for s in _auto_snaps(mgr)] == ["s-1"]                # 锁内重新读、照常拍
    assert mgr.auto_snapshot_status()["failures"] == 0                            # 锁外读失败不计


def test_session_hook_swallows_a_failure_in_recording_the_failure(tmp_path, monkeypatch, caplog):
    """T2 review M-2：记失败本身也出错（例如日志 handler 抛异常）时，钩子的最外层兜底记 exception、照常
    返回，坏掉的钩子不会变成摄入 job 的失败；manager.lock 照样放开。只有 SessionSnapshotDeferred 往外抛。"""
    mgr, _state = _auto_manager(tmp_path, _Clock())
    mgr.set_auto_snapshot("default", True, 0)

    def _no_space(*_args, **_kwargs):
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")

    def _broken_record(*_args, **_kwargs):
        raise RuntimeError("logging handler exploded")
    monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
    monkeypatch.setattr(mgr, "_record_auto_failure", _broken_record)
    with caplog.at_level("ERROR", logger="starling.dashboard.world_manager"):
        assert mgr._make_session_hook()("s-1") is None
    assert "session auto snapshot hook failed" in caplog.text
    assert "logging handler exploded" in caplog.text
    assert _manager_lock_is_free(mgr), "兜底之前 _auto_snapshot 的 finally 必须已放开 manager.lock"


def test_start_workers_attaches_the_session_hook_before_threads_start(tmp_path, monkeypatch):
    mgr, state = _auto_manager(tmp_path, _Clock())
    engine = state.engine
    assert engine.session_snapshot_hook is None
    seen = {}
    real_start = engine.start_ingest_worker

    def _spy_start(*args, **kwargs):
        seen["hook"] = engine.session_snapshot_hook
        return real_start(*args, **kwargs)
    monkeypatch.setattr(engine, "start_ingest_worker", _spy_start)
    mgr.workers_enabled = True
    mgr._start_workers(engine, state.config)
    try:
        assert seen["hook"] is not None, "摄入线程起来之前钩子就要挂上"
        assert engine.session_snapshot_hook is not None
        mgr.set_auto_snapshot("default", True, 0)
        assert engine.session_snapshot_hook("s-9") is None
        assert [s["session_id"] for s in _auto_snaps(mgr)] == ["s-9"]
    finally:
        assert engine.quiesce()


# ── 按时间的调度线程 ──

def test_scheduler_rounds_run_on_their_own_thread(tmp_path, monkeypatch):
    clock = _Clock("2026-10-28T00:00:00Z")
    mgr, state = _auto_manager(tmp_path, clock)
    _remember(state.engine, "Bob owns auth")
    rounds = _Rounds(mgr, monkeypatch)
    before = set(_scheduler_threads())
    mgr.start_scheduler(poll_s=0.01, first_delay_s=0)
    started = [t for t in _scheduler_threads() if t not in before]
    try:
        assert len(started) == 1 and started[0].daemon
        assert rounds.step().startswith("taken:")
        assert rounds.step() == "skipped:not_due"                # 时钟没推进
        clock.now = "2026-10-29T01:00:00Z"
        _remember(state.engine, "Carol owns billing")
        assert rounds.step().startswith("taken:")
        assert rounds.thread_names == {"starling-auto-snapshot"}
        snaps = _auto_snaps(mgr)
        assert [(s["reason"], s["created_at"]) for s in snaps] == [
            ("interval", "2026-10-29T01:00:00Z"), ("interval", "2026-10-28T00:00:00Z")]
        # M-5：taken / skipped 轮次之后锁已放开。要趁调度线程还活着查（它此刻停在下一轮的闸门上）：
        # 线程退出之后，探测线程可能复用它的线程 ident，RLock 会把漏放的锁当成探测线程重入，照样取到。
        assert started[0].is_alive()
        assert _manager_lock_is_free(mgr), "调度线程的 taken / skipped 轮次之后 manager.lock 必须已放开"
    finally:
        rounds.close()
        mgr.stop_scheduler()
    assert all(not t.is_alive() for t in started)


def test_scheduler_skips_a_busy_round_and_takes_the_next(tmp_path, monkeypatch):
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    rounds = _Rounds(mgr, monkeypatch)
    mgr.start_scheduler(poll_s=0.01, first_delay_s=0)
    try:
        holder, release = _hold_manager_lock(mgr)
        try:
            assert rounds.step() == "busy"
        finally:
            release.set()
            holder.join(10)
        assert _auto_snaps(mgr) == []
        assert rounds.step().startswith("taken:")
        assert mgr.auto_snapshot_status()["failures"] == 0
    finally:
        rounds.close()
        mgr.stop_scheduler()


def test_stop_scheduler_joins_the_thread(tmp_path):
    mgr, _state = _auto_manager(tmp_path, _Clock())
    before = set(_scheduler_threads())
    mgr.start_scheduler(poll_s=3600.0, first_delay_s=3600.0)
    started = [t for t in _scheduler_threads() if t not in before]
    assert len(started) == 1
    mgr.stop_scheduler()
    assert not started[0].is_alive()
    mgr.stop_scheduler()                                         # 再停一次是 no-op


def test_start_scheduler_twice_runs_one_thread(tmp_path):
    mgr, _state = _auto_manager(tmp_path, _Clock())
    before = set(_scheduler_threads())
    mgr.start_scheduler(poll_s=3600.0, first_delay_s=3600.0)
    mgr.start_scheduler(poll_s=3600.0, first_delay_s=3600.0)
    try:
        assert len([t for t in _scheduler_threads() if t not in before]) == 1
    finally:
        mgr.stop_scheduler()
    assert [t for t in _scheduler_threads() if t not in before] == []


def test_disable_workers_also_stops_the_scheduler(tmp_path):
    mgr, _state = _auto_manager(tmp_path, _Clock())
    mgr.workers_enabled = True
    before = set(_scheduler_threads())
    mgr.start_scheduler(poll_s=3600.0, first_delay_s=3600.0)
    started = [t for t in _scheduler_threads() if t not in before]
    assert len(started) == 1
    mgr.disable_workers()
    assert mgr.workers_enabled is False
    assert not started[0].is_alive()


class _LockWithEnterHook:
    """包住一把 Lock：with 进入、拿到锁之后先调 on_enter()，用来在「已持锁」这一刻确定性地插一步。"""

    def __init__(self, inner, on_enter) -> None:
        self._inner, self._on_enter = inner, on_enter

    def __enter__(self):
        self._inner.acquire()
        try:
            self._on_enter()
        except BaseException:
            self._inner.release()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self._inner.release()


class _StopThatAlsoWakes:
    """调度线程停止事件的替身（只给 stop_scheduler 用）：set() 先 set 真事件，再叫醒在闸门上等的那一轮。
    线程自己持有的仍是真事件，循环的 stop.wait 不变。"""

    def __init__(self, real: threading.Event, wake: threading.Event) -> None:
        self._real, self._wake = real, wake

    def set(self) -> None:
        self._real.set()
        self._wake.set()


def test_disable_workers_stops_the_scheduler_before_taking_the_locks(tmp_path, monkeypatch):
    """T2 review M-1：关停一开始就停调度线程，不留「锁已放开、停止事件还没 set」的窗口。

    调度线程的每一轮先停在闸门上（poll 将要到期），闸门由测试放行（fire）或由停止事件叫醒。关停拿到
    _build_lock（manager.lock 已在手）那一刻：调度线程若还活着（旧顺序：先取锁、后停线程），就在此刻
    放行，也就是旧顺序窗口里 poll 恰好到期，让那一轮真的开起来。新顺序下此刻线程已退出，什么都开
    不起来。断言：关停开始之后没有开过一轮，取锁时线程已退出，返回后线程不在、锁已放开。"""
    mgr, state = _auto_manager(tmp_path, _Clock())
    _remember(state.engine, "Bob owns auth")
    mgr.workers_enabled = True
    real_round = mgr._auto_snapshot
    parked, fire, round_began = threading.Event(), threading.Event(), threading.Event()
    real_stop: list = []
    began: list = []

    def _gated_round(kind, session_id="", **kwargs):
        parked.set()
        assert fire.wait(30)
        if real_stop[0].is_set():
            return "stopped"                 # 停止事件叫醒的：这一轮不开
        began.append(kind)
        round_began.set()
        return real_round(kind, session_id, **kwargs)
    monkeypatch.setattr(mgr, "_auto_snapshot", _gated_round)

    before = set(_scheduler_threads())
    mgr.start_scheduler(poll_s=0.01, first_delay_s=0)
    started = [t for t in _scheduler_threads() if t not in before]
    assert len(started) == 1
    thread = started[0]
    real_stop.append(mgr._scheduler_stop)
    mgr._scheduler_stop = _StopThatAlsoWakes(real_stop[0], fire)
    alive_at_locks: list = []

    def _on_locks_taken():
        alive = thread.is_alive()
        alive_at_locks.append(alive)
        if alive:
            fire.set()                       # 旧顺序的窗口：poll 恰在此刻到期
            assert round_began.wait(10)

    real_build_lock = mgr._build_lock
    try:
        assert parked.wait(10), "调度线程没走到将要开的那一轮"
        mgr._build_lock = _LockWithEnterHook(real_build_lock, _on_locks_taken)
        mgr.disable_workers()
        assert alive_at_locks == [False], "disable_workers 取锁时调度线程还活着"
        assert began == [], "关停开始之后调度线程又开了一轮"
        assert _auto_snaps(mgr) == []
        assert not thread.is_alive()
        assert mgr.workers_enabled is False
        assert mgr._scheduler is None and mgr._scheduler_stop is None
        assert _manager_lock_is_free(mgr)
    finally:
        mgr._build_lock = real_build_lock
        real_stop[0].set()
        fire.set()
        mgr.stop_scheduler()
        thread.join(10)
    assert not thread.is_alive()


# ───────────────────────── W4 T3：摄入调用点、路由、lifespan 接线 ─────────────────────────

def _user_line(text: str, ts: str = "2026-10-27T09:00:00.000Z") -> str:
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def _assistant_line(text: str, ts: str = "2026-10-27T09:00:05.000Z") -> str:
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def _lane_app(tmp_path, *, lane: bool = True):
    """真实引擎 + create_app 的 WorldManager：左脑 lane 开启时把 stub 嵌入当作标定模型（lane 生效，
    同 test_ingest_source_lane），spool 在 tmp_path 下（_cfg 的 ingest_spool_path），抽取用假 LLM。
    挂上会话钩子，并打开默认世界的按会话触发（默认世界默认只按时间）。不进 lifespan，不起线程。"""
    cfg = _cfg(tmp_path)
    cfg.source_lane_enabled = lane
    if lane:
        cfg.source_lane_model, cfg.source_lane_dim = "stub", 8
    eng = DashboardEngine(cfg)
    app = create_app(cfg, engine=eng)
    mgr = app.state.worlds
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    eng.llm = fake
    mgr.attach_hooks(eng)
    mgr.set_auto_snapshot("default", True, 0)
    return TestClient(app), app, eng, mgr


def _spool_job(tmp_path, eng, job_name: str, session_id: str | None):
    """往引擎的 spool 分区放一个 transcript 摄入 job，返回 (job 路径, job dict)。"""
    transcript = tmp_path / f"{job_name}.jsonl"
    transcript.write_text("\n".join([_user_line(f"{job_name}: Bob owns auth"),
                                     _assistant_line("noted")]))
    job = {"transcript_path": str(transcript), "cwd": "/proj/foo", "tenant": eng._cfg.tenant}
    if session_id is not None:
        job["session_id"] = session_id
    spool = eng._ingest_spool
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f"{job_name}.json"
    path.write_text(json.dumps(job))
    return path, job


def _session_units(db_path: str, session_id: str) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM source_unit_meta WHERE session_id=?",
                           (session_id,)).fetchone()[0]
    finally:
        con.close()


def _engine_lock_is_free(eng) -> bool:
    """在调用它的线程里非阻塞取一次引擎锁：取得到说明别的线程都没持有它。"""
    if eng._lock.acquire(blocking=False):
        eng._lock.release()
        return True
    return False


# ── 摄入端到端 ──

def test_ingest_snapshots_a_new_session_before_writing_it_and_only_once(tmp_path):
    client, _app, eng, mgr = _lane_app(tmp_path)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    snaps = _auto_snaps(mgr)
    assert [(s["kind"], s["reason"], s["session_id"]) for s in snaps] == [("auto", "session", "s-1")]
    assert _session_units(snaps[0]["path"], "s-1") == 0, "快照必须在这个会话写入之前拍"
    assert _session_units(eng._db_path, "s-1") == 2              # 写入照常完成
    assert (eng._ingest_spool / "done" / "job-1.json").exists()

    _spool_job(tmp_path, eng, "job-2", "s-1")                     # 同一会话的第二个 job
    assert eng._ingest_drain_once() == "done"
    assert len(_auto_snaps(mgr)) == 1, "同一会话不再拍"

    _spool_job(tmp_path, eng, "job-3", "s-2")                     # 新会话再拍一份
    assert eng._ingest_drain_once() == "done"
    newest = _auto_snaps(mgr)[0]
    assert (newest["reason"], newest["session_id"]) == ("session", "s-2")
    # s-1 的两个 job 各写了两条话轮；s-2 还没写。
    assert _session_units(newest["path"], "s-1") == 4 and _session_units(newest["path"], "s-2") == 0

    # 会话清单里的锚点就是这两份快照。
    rows = {row["session_id"]: row for row in
            client.get("/api/worlds/default/sessions").json()["sessions"]}
    assert rows["s-1"]["anchor"]["snapshot_id"] == snaps[0]["id"]
    assert rows["s-2"]["anchor"]["snapshot_id"] == newest["id"]


def test_ingest_calls_the_session_hook_outside_the_engine_lock_and_before_any_write(tmp_path):
    _client, _app, eng, _mgr = _lane_app(tmp_path)
    calls = []

    def _spy(session_id):
        # 在另一个线程里取引擎锁：摄入线程若持着它，这里取不到。
        _worker, free = _call_in_thread(_engine_lock_is_free, eng)
        calls.append((session_id, free.get("value"), _session_units(eng._db_path, session_id),
                      _count(eng._db_path)))
    eng.session_snapshot_hook = _spy
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert calls == [("s-1", True, 0, 0)]
    # 没有会话 id：不调钩子。这种 job 随后在 retain_source_turns 里失败（session_id 必须非空，
    # 本任务之前就是如此），走有界重试；这里只钉住钩子没被调。
    for job_name, session_id in (("job-2", ""), ("job-3", None)):
        job_path, _job = _spool_job(tmp_path, eng, job_name, session_id)
        assert eng._ingest_drain_once() == "deferred"
        job_path.unlink()
    assert calls == [("s-1", True, 0, 0)]


def test_ingest_defers_the_job_without_an_attempt_while_the_manager_lock_is_held(tmp_path):
    _client, _app, eng, mgr = _lane_app(tmp_path)
    job_path, job = _spool_job(tmp_path, eng, "job-1", "s-1")
    holder, release = _hold_manager_lock(mgr)
    try:
        drainer, result = _call_in_thread(eng._ingest_drain_once)
        assert not drainer.is_alive(), "摄入线程在 manager.lock 上等了"
        assert result == {"value": "deferred"}
    finally:
        release.set()
        holder.join(10)
    assert json.loads(job_path.read_text()) == job, "放回 spool 的 job 原样不变（不计 attempts）"
    assert not job_path.with_suffix(".json.processing").exists()
    assert _session_units(eng._db_path, "s-1") == 0 and _count(eng._db_path) == 0   # 库里没有写入
    assert _auto_snaps(mgr) == []
    assert mgr.auto_snapshot_status()["failures"] == 0           # busy 不是失败

    assert eng._ingest_drain_once() == "done"                     # 锁放开后照常拍、照常写
    snaps = _auto_snaps(mgr)
    assert [s["session_id"] for s in snaps] == ["s-1"]
    assert _session_units(snaps[0]["path"], "s-1") == 0
    assert _session_units(eng._db_path, "s-1") == 2 and _count(eng._db_path) >= 1


def test_ingest_is_not_deferred_by_the_manager_lock_when_on_session_is_off(tmp_path):
    """T2 review M-3（端到端）：默认世界默认按会话关。manager.lock 被无关的管理操作占着时，摄入 job 照常
    写完，不被放回 spool，也不拍快照。"""
    _client, _app, eng, mgr = _lane_app(tmp_path)
    mgr.set_auto_snapshot("default", False, 24)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    holder, release = _hold_manager_lock(mgr)
    try:
        drainer, result = _call_in_thread(eng._ingest_drain_once)
        assert not drainer.is_alive(), "摄入线程在 manager.lock 上等了"
        assert result == {"value": "done"}, result
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()
    assert (eng._ingest_spool / "done" / "job-1.json").exists()
    assert _session_units(eng._db_path, "s-1") == 2
    assert _auto_snaps(mgr) == []
    assert mgr.auto_snapshot_status()["failures"] == 0


def test_ingest_continues_when_the_session_snapshot_fails(tmp_path, monkeypatch):
    client, _app, eng, mgr = _lane_app(tmp_path)

    def _no_space(*_args, **_kwargs):
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
    monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert (eng._ingest_spool / "done" / "job-1.json").exists()
    assert _session_units(eng._db_path, "s-1") == 2 and _count(eng._db_path) >= 1
    assert _auto_snaps(mgr) == []

    body = client.get("/api/ingest_status").json()
    assert body["done"] == 1 and body["failed"] == 0
    assert body["auto_snapshot_failures"] == 1
    assert body["auto_snapshot_last_error"] == "insufficient_disk"
    status = client.get("/api/worlds").json()["auto_snapshot_status"]
    assert (status["failures"], status["last_error"]) == (1, "insufficient_disk")
    assert _SECONDS_ISO.fullmatch(status["last_error_at"])


def test_ingest_discards_the_job_when_memory_is_cleared_during_the_session_snapshot(tmp_path):
    """快照可能要几秒：这期间发生的清空让 job 作废，与钩子之前的代数检查同一语义，来源表一行不写。"""
    _client, _app, eng, mgr = _lane_app(tmp_path)
    real_hook = eng.session_snapshot_hook

    def _cleared_during_snapshot(session_id):
        real_hook(session_id)
        with eng._lock:
            eng._clear_generation += 1
    eng.session_snapshot_hook = _cleared_during_snapshot
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert (eng._ingest_spool / "discarded" / "job-1.json").exists()
    assert _session_units(eng._db_path, "s-1") == 0
    assert len(_auto_snaps(mgr)) == 1


def test_deferred_job_is_discarded_not_requeued_when_memory_is_cleared_during_the_hook(tmp_path):
    """整支审查 I-1：job claim 之后、钩子推迟之前发生了清空。clear_memory 只丢弃 spool 根下的 pending
    job，看不到这个 `.processing`；推迟分支若原样放回，下一轮 drain 会用新代数重新 claim，把清空前的
    会话写回库里。必须改进 discarded/，与 _TenantCleared 同一归宿。"""
    _client, _app, eng, mgr = _lane_app(tmp_path)
    real_hook = eng.session_snapshot_hook
    calls = []

    def _clear_then_defer(session_id):
        calls.append(session_id)
        if len(calls) == 1:
            eng.clear_memory(confirm=eng._cfg.tenant)      # 用户在钩子的窗口里点了清空
            raise SessionSnapshotDeferred("manager lock busy")
        real_hook(session_id)                               # 之后的调用照常拍、照常写
    eng.session_snapshot_hook = _clear_then_defer
    job_path, _job = _spool_job(tmp_path, eng, "job-1", "s-1")

    assert eng._ingest_drain_once() == "done"
    spool = eng._ingest_spool
    assert (spool / "discarded" / "job-1.json").exists()
    assert not job_path.exists(), "清空之后不得放回 spool 根"
    assert not job_path.with_suffix(".json.processing").exists()

    assert eng._ingest_drain_once() == "empty"             # 再 drain 一轮：没有东西可重新灌回来
    assert calls == ["s-1"]
    assert _session_units(eng._db_path, "s-1") == 0 and _count(eng._db_path) == 0
    assert _auto_snaps(mgr) == []


def test_clear_memory_cannot_land_between_the_generation_check_and_the_source_write(
        tmp_path, monkeypatch):
    """Codex C1（W4 之前就有的竞态）：代数检查与 retain_source_turns 之间若放开 engine._lock，另一个
    线程的 clear_memory 会落在两者之间，清空前的 transcript 在清空之后写回来源表。两者必须同一把锁：
    写入进行时清空只能等，等它拿到锁，刚写下的来源行随清空一起删掉。

    必须两个真线程：engine._lock 是 RLock，在同一线程里调 clear_memory 会直接重入，看不出竞态。"""
    _client, _app, eng, _mgr = _lane_app(tmp_path)
    import starling.dashboard.engine as engine_mod
    real_retain = engine_mod._core.retain_source_turns
    in_retain, may_write = threading.Event(), threading.Event()

    def _paused_retain(*args, **kwargs):
        in_retain.set()                        # 代数检查已过，马上写来源行
        assert may_write.wait(10), "测试没有放行写入"
        return real_retain(*args, **kwargs)
    monkeypatch.setattr(engine_mod._core, "retain_source_turns", _paused_retain)
    _spool_job(tmp_path, eng, "job-1", "s-1")

    drain_result: dict = {}

    def _drain():
        try:
            drain_result["value"] = eng._ingest_drain_once()
        except BaseException as exc:  # noqa: BLE001 — 测试要看见 drain 抛了什么
            drain_result["error"] = exc

    drainer = threading.Thread(target=_drain, daemon=True)
    clearer = None
    try:
        drainer.start()
        assert in_retain.wait(10), "drain 没走到 retain_source_turns"
        clear_result: dict = {}

        def _clear():
            try:
                clear_result["value"] = eng.clear_memory(confirm=eng._cfg.tenant)
            except BaseException as exc:  # noqa: BLE001
                clear_result["error"] = exc

        clearer = threading.Thread(target=_clear, daemon=True)
        clearer.start()
        clearer.join(timeout=0.2)
        assert clearer.is_alive(), "retain_source_turns 进行中 clear_memory 不该拿到 engine._lock"
        assert clear_result == {}
    finally:
        may_write.set()
        drainer.join(10)
        if clearer is not None:
            clearer.join(10)
    assert not drainer.is_alive() and clearer is not None and not clearer.is_alive()
    assert "error" not in clear_result, clear_result
    assert "error" not in drain_result, drain_result
    # 清空排在写入之后：刚写下的来源行随清空一起删掉，清空之后库里没有这个会话。
    assert _session_units(eng._db_path, "s-1") == 0
    # 记录 drain 的去向（报告用；不作断言）：清空落在 remember 之前 → 代数过期 → discarded/。
    print("C1 drain outcome:", drain_result,
          sorted(p.relative_to(eng._ingest_spool).as_posix()
                 for p in eng._ingest_spool.rglob("*.json")))


def _stuck_retention_registry(mgr) -> tuple[str, str]:
    """Codex r2 R2-2 的真实卡住场景：默认世界拍 4 份手动快照（m1 最旧），保留份数调到 2，把第二旧的
    那份（m2）换成指向自己的符号链接（解析时 ELOOP，修剪删不掉它）。修剪从最旧起：先删掉正常的
    m1，再卡在 m2 上。返回 (m1, m2) 的 id。"""
    ids = [mgr.take_snapshot("default", f"m{index}")["id"] for index in range(1, 5)]
    reg = mgr.load()
    reg["snapshot_retention"] = 2
    mgr.save(reg)
    stuck = next(s for s in mgr.load()["snapshots"]["default"] if s["id"] == ids[1])["path"]
    os.remove(stuck)
    os.symlink(os.path.basename(stuck), stuck)
    return ids[0], ids[1]


def test_refused_auto_snapshot_saves_the_partial_prune_and_counts_snapshot_failed(tmp_path):
    """Codex r2 R2-2：修剪先删掉正常 victim 的文件、再卡住时，C++ 返回 refused 与部分修剪后的注册表。
    Python 必须存下它（worlds.json 里没有文件已不在的条目），按 snapshot_failed 计一次失败，摄入
    job 照常写完。"""
    client, _app, eng, mgr = _lane_app(tmp_path)
    normal, stuck = _stuck_retention_registry(mgr)
    normal_path = next(s for s in mgr.load()["snapshots"]["default"] if s["id"] == normal)["path"]
    assert os.path.exists(normal_path)

    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert (eng._ingest_spool / "done" / "job-1.json").exists()      # 摄入不因拒绝而推迟或失败
    assert _session_units(eng._db_path, "s-1") == 2

    saved = [s["id"] for s in mgr.load()["snapshots"]["default"]]
    assert normal not in saved, "部分修剪后的注册表必须存下：正常 victim 的条目已摘"
    assert not os.path.exists(normal_path)
    assert stuck in saved and len(saved) == 3
    assert _auto_snaps(mgr) == []                                      # 没拍新的
    status = mgr.auto_snapshot_status()
    assert (status["failures"], status["last_error"]) == (1, "snapshot_failed")
    body = client.get("/api/ingest_status").json()
    assert (body["auto_snapshot_failures"], body["auto_snapshot_last_error"]) == (1, "snapshot_failed")
    assert _manager_lock_is_free(mgr)

    # 按时间触发同样拒绝；注册表已存过，没有可删的正常 victim，列表不再变。
    mgr.set_auto_snapshot("default", True, 1)
    assert mgr._auto_snapshot("interval") == "failed"
    assert [s["id"] for s in mgr.load()["snapshots"]["default"]] == saved
    assert mgr.auto_snapshot_status()["failures"] == 2


def test_refused_auto_snapshot_whose_save_fails_is_still_one_counted_failure(tmp_path, monkeypatch):
    """拒绝之后存注册表本身失败：外层照常记一次失败（internal_error），不外抛；没有新快照文件要补偿。"""
    _client, _app, _eng, mgr = _lane_app(tmp_path)
    _stuck_retention_registry(mgr)
    files_before = sorted(os.listdir(mgr.load()["worlds"]["default"]["snapshot_dir"]))

    def _disk_gone(_reg):
        raise OSError("registry write failed")
    monkeypatch.setattr(mgr, "save", _disk_gone)
    assert mgr._auto_snapshot("session", "s-1") == "failed"
    status = mgr.auto_snapshot_status()
    assert (status["failures"], status["last_error"]) == (1, "internal_error")
    # 正常 victim 的文件已被修剪删掉（只少这一个），没有多出新快照文件。
    files_after = sorted(os.listdir(mgr.load()["worlds"]["default"]["snapshot_dir"]))
    assert len(files_after) == len(files_before) - 1 and set(files_after) <= set(files_before)
    assert _manager_lock_is_free(mgr)


def test_binding_take_auto_snapshot_refused_shape(tmp_path):
    """绑定的 refused：拒绝时是原因字符串、snapshot 与 skipped 为 None、registry 是部分修剪后的；
    其余结果 refused 为 None。"""
    _client, _app, _eng, mgr = _lane_app(tmp_path)
    normal, stuck = _stuck_retention_registry(mgr)
    reg = mgr.load()
    out = _core.world_take_auto_snapshot(reg, "default", "session", "2026-10-28T08:00:00Z", "s-1")
    assert out["snapshot"] is None and out["skipped"] is None
    assert out["refused"].startswith("retention cannot prune")
    ids = [s["id"] for s in out["registry"]["snapshots"]["default"]]
    assert normal not in ids and stuck in ids
    assert normal in [s["id"] for s in reg["snapshots"]["default"]]      # 入参 dict 不变
    skipped = _core.world_take_auto_snapshot(out["registry"], "default", "session",
                                             "2026-10-28T08:00:00Z", "s-x", vector_backend="zvec")
    assert skipped["refused"] is None and skipped["skipped"] == "unsupported_backend"


def test_ingest_with_the_lane_off_takes_no_session_snapshot(tmp_path):
    """调用点在来源通道的写入里：lane 关闭就没有会话，也就没有按会话的快照（设计如此）。"""
    _client, _app, eng, mgr = _lane_app(tmp_path, lane=False)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert _auto_snaps(mgr) == []
    assert _count(eng._db_path) >= 1


# ── 路由 ──

def test_routes_put_auto_snapshot_writes_and_rejects(tmp_path):
    client, app = _client(tmp_path)
    mgr = app.state.worlds
    r = client.put("/api/worlds/default/auto_snapshot", json={"on_session": True, "interval_hours": 6})
    assert r.status_code == 200
    assert r.json() == {"world": "default", "auto_snapshot": {"on_session": True, "interval_hours": 6}}
    assert mgr.load()["worlds"]["default"]["auto_snapshot"] == {"on_session": True, "interval_hours": 6}
    row = client.get("/api/worlds").json()["worlds"][0]
    assert row["auto_snapshot"] == {"on_session": True, "interval_hours": 6}

    before = mgr.load()
    for hours in (8761, -1, 2 ** 70):
        r = client.put("/api/worlds/default/auto_snapshot",
                       json={"on_session": False, "interval_hours": hours})
        assert (r.status_code, r.json()["detail"]) == (400, "invalid_auto_snapshot"), hours
    r = client.put("/api/worlds/nope/auto_snapshot", json={"on_session": True, "interval_hours": 0})
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    assert client.put("/api/worlds/default/auto_snapshot", json={"on_session": True}).status_code == 422
    assert mgr.load() == before


def test_routes_auto_snapshot_body_is_strictly_typed(tmp_path):
    """整支审查 M-2：宽松模式会把 "yes" 转成 True、true 转成 1、5.0 转成 5。严格类型下这些都回 422，
    注册表不变；PUT 与新建世界带的 auto_snapshot 是同一个模型，两处都钉住。"""
    client, app = _client(tmp_path)
    mgr = app.state.worlds
    before = mgr.load()
    bad_bodies = ({"on_session": "yes", "interval_hours": 5},
                  {"on_session": True, "interval_hours": True},
                  {"on_session": True, "interval_hours": 5.0},
                  {"on_session": True, "interval_hours": "7"},
                  {"on_session": 1, "interval_hours": 5})
    for body in bad_bodies:
        r = client.put("/api/worlds/default/auto_snapshot", json=body)
        assert r.status_code == 422, (body, r.json())
        r = client.post("/api/worlds", json={"name": "sim-strict", "auto_snapshot": body})
        assert r.status_code == 422, (body, r.json())
    assert mgr.load() == before

    r = client.put("/api/worlds/default/auto_snapshot", json={"on_session": False, "interval_hours": 5})
    assert r.status_code == 200
    assert r.json()["auto_snapshot"] == {"on_session": False, "interval_hours": 5}
    r = client.post("/api/worlds", json={"name": "sim-strict",
                                         "auto_snapshot": {"on_session": True, "interval_hours": 0}})
    assert r.status_code == 200, r.json()
    assert mgr.load()["worlds"]["sim-strict"]["auto_snapshot"] == {"on_session": True, "interval_hours": 0}


def test_routes_create_world_with_and_without_auto_snapshot(tmp_path):
    client, app = _client(tmp_path)
    plain = client.post("/api/worlds", json={"name": "sim-1"}).json()
    assert plain["auto_snapshot"] == {"on_session": True, "interval_hours": 0}     # 新世界默认值
    custom = client.post("/api/worlds", json={"name": "sim-2", "label": "x",
                                              "auto_snapshot": {"on_session": False,
                                                                "interval_hours": 12}}).json()
    assert custom["auto_snapshot"] == {"on_session": False, "interval_hours": 12}
    assert app.state.worlds.load()["worlds"]["sim-2"]["auto_snapshot"] == {"on_session": False,
                                                                           "interval_hours": 12}
    r = client.post("/api/worlds", json={"name": "sim-3",
                                         "auto_snapshot": {"on_session": True, "interval_hours": 9000}})
    assert (r.status_code, r.json()["detail"]) == (400, "invalid_auto_snapshot")
    assert "sim-3" not in app.state.worlds.load()["worlds"]
    rows = {w["name"]: w for w in client.get("/api/worlds").json()["worlds"]}
    assert rows["default"]["auto_snapshot"] == {"on_session": False, "interval_hours": 24}
    assert rows["sim-1"]["auto_snapshot"] == {"on_session": True, "interval_hours": 0}


def test_routes_list_worlds_carries_auto_snapshot_status(tmp_path):
    client, _app = _client(tmp_path)
    body = client.get("/api/worlds").json()
    assert set(body) == {"active", "default", "snapshot_retention", "auto_snapshot_status", "worlds"}
    assert body["auto_snapshot_status"] == {"failures": 0, "last_error": None, "last_error_at": None}


def _sim_world_with_sessions(app) -> dict:
    """在非活动世界 sim-1 里写三个会话（s-1 先有一份按会话快照作锚点，s-null 的 observed_at 全为
    NULL），返回锚点快照。非活动世界没有引擎，测试自己开一条写连接再关掉。"""
    mgr = app.state.worlds
    mgr.create("sim-1", "")
    reg = mgr.load()
    db = reg["worlds"]["sim-1"]["db_path"]
    adapter = _core.SqliteAdapter.open(db)
    out = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z", "s-1")
    mgr.save(out["registry"])
    for session_id in ("s-1", "s-2", "s-null"):
        _retain_session(adapter, "sim-1", session_id)
    del adapter
    gc.collect()
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE source_unit_meta SET observed_at=NULL WHERE session_id='s-null'")
        con.commit()
    finally:
        con.close()
    return out["snapshot"]


def test_routes_sessions_shape_anchor_and_null_times(tmp_path):
    client, app = _client(tmp_path)
    anchor = _sim_world_with_sessions(app)
    r = client.get("/api/worlds/sim-1/sessions")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"world", "source_lane_enabled", "total", "null_observed", "truncated",
                         "sessions"}
    assert (body["world"], body["source_lane_enabled"]) == ("sim-1", False)    # _cfg 关着 lane
    assert (body["total"], body["null_observed"], body["truncated"]) == (3, 2, False)
    rows = {row["session_id"]: row for row in body["sessions"]}
    assert rows["s-1"] == {"session_id": "s-1", "units": 2, "speakers": 2,
                           "first_at": "2026-10-27T09:00:00Z", "last_at": "2026-10-27T09:00:00Z",
                           "anchor": {"snapshot_id": anchor["id"],
                                      "created_at": "2026-10-28T08:00:00Z"}}
    assert rows["s-2"]["anchor"] is None
    assert (rows["s-null"]["first_at"], rows["s-null"]["last_at"]) == (None, None)

    limited = client.get("/api/worlds/sim-1/sessions", params={"limit": 1}).json()
    assert (limited["total"], limited["truncated"], len(limited["sessions"])) == (3, True, 1)
    app.state.config.source_lane_enabled = True                   # 取当前 config，不论哪个世界
    assert client.get("/api/worlds/sim-1/sessions").json()["source_lane_enabled"] is True


def test_routes_sessions_maps_empty_cpp_strings_to_null(tmp_path, monkeypatch):
    """C++ 的 SessionRow 没有锚点、没有时间时是空串；路由按契约给 null，不依赖绑定已经转好。"""
    client, _app = _client(tmp_path)

    def _raw(_reg, _world, _limit):
        return {"total": 1, "null_observed": 2, "truncated": False,
                "sessions": [{"session_id": "s-x", "units": 2, "speakers": 1, "first_at": "",
                              "last_at": "", "anchor": {"snapshot_id": "", "created_at": ""},
                              "extra": "dropped"}]}
    monkeypatch.setattr(_core, "world_list_sessions", _raw)
    body = client.get("/api/worlds/default/sessions").json()
    assert body["sessions"] == [{"session_id": "s-x", "units": 2, "speakers": 1, "first_at": None,
                                 "last_at": None, "anchor": None}]


def test_routes_sessions_limit_404_and_503(tmp_path):
    client, app = _client(tmp_path)
    assert client.get("/api/worlds/default/sessions").json()["sessions"] == []
    assert client.get("/api/worlds/default/sessions", params={"limit": 1000}).status_code == 200
    for limit in (0, 1001, -5):
        # FastAPI 在 C++ 之前回 422：越界的 limit 不该显示成「自动快照设置无效」（T4 m2）。
        assert client.get("/api/worlds/default/sessions",
                          params={"limit": limit}).status_code == 422, limit
    r = client.get("/api/worlds/nope/sessions")
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    _force_switching(app.state.worlds, True)
    try:
        r = client.get("/api/worlds/default/sessions")
        assert (r.status_code, r.json()["detail"]) == (503, "world_switching")
    finally:
        _force_switching(app.state.worlds, False)


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/worlds/default/sessions", None),
    ("put", "/api/worlds/default/auto_snapshot", {"on_session": True, "interval_hours": 0}),
])
def test_w4_routes_503_when_world_manager_absent(method, path, body):
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    kwargs = {"json": body} if body is not None else {}
    r = getattr(TestClient(app), method)(path, **kwargs)
    assert (r.status_code, r.json()["detail"]) == (503, "worlds_unavailable")


def test_routes_snapshot_rows_carry_w4_fields_and_never_content_mark(tmp_path):
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    _remember(app.state.engine, "Bob owns auth")                 # 有内容，内容标记才非空
    mgr = app.state.worlds
    manual = client.post("/api/worlds/default/snapshots", json={"note": "m"})
    assert "content_mark" not in manual.text
    assert (manual.json()["sessions_complete"], manual.json()["session_id"]) == (0, None)
    mgr.set_auto_snapshot("default", True, 0)
    assert mgr._make_session_hook()("s-1") is None
    listed = client.get("/api/worlds/default/snapshots")
    assert "content_mark" not in listed.text
    rows = {row["reason"]: row for row in listed.json()["snapshots"]}
    assert (rows["session"]["kind"], rows["session"]["session_id"]) == ("auto", "s-1")
    assert rows["session"]["sessions_complete"] == 0
    assert rows["manual"]["session_id"] is None
    assert "content_mark" not in client.get("/api/worlds").text
    assert all("content_mark" in snap for snap in mgr.load()["snapshots"]["default"])   # 只在内部


def test_snapshot_row_of_a_pre_w4_entry_has_null_w4_fields(tmp_path):
    from starling.dashboard.routes.worlds import _snapshot_row
    legacy = {"id": "20261025T011200Z-a1b2", "kind": "auto", "reason": "session", "note": "",
              "created_at": "2026-10-25T01:12:00Z", "world_time": "", "size_bytes": 10,
              "path": str(tmp_path / "gone.db")}
    row = _snapshot_row(legacy)
    assert (row["sessions_complete"], row["session_id"]) == (None, None)
    assert "content_mark" not in row and "path" not in row


def test_routes_list_a_pre_w4_snapshot_entry_with_null_w4_fields(tmp_path):
    """T2 review：注册表 dict 只在字段有值时带 sessions_complete / session_id；W4 之前的条目两个都没有，
    列表照常列出，两个字段回 null（不能 KeyError 成 500）。"""
    client, app = _client(tmp_path)
    app.state.engine = DashboardEngine(app.state.config)
    mgr = app.state.worlds
    snap = mgr.take_snapshot("default", "w3")
    reg = mgr.load()
    entry = reg["snapshots"]["default"][0]
    for key in ("sessions_complete", "session_id", "content_mark"):
        entry.pop(key, None)                                     # 退回 W3 写下的形状
    mgr.save(reg)
    stored = mgr.load()["snapshots"]["default"][0]
    assert "sessions_complete" not in stored and "session_id" not in stored
    for path in ("/api/worlds/default/snapshots",):
        r = client.get(path)
        assert r.status_code == 200
        (row,) = r.json()["snapshots"]
        assert row["id"] == snap["id"]
        assert (row["sessions_complete"], row["session_id"]) == (None, None)
        assert "content_mark" not in row
    assert client.get("/api/worlds").status_code == 200


def test_ingest_job_without_a_session_id_never_reaches_the_real_hook(tmp_path):
    """T2 review：空 session_id 交给 C++ 会抛 invalid_auto_snapshot、记成失败；调用点必须先挡掉。"""
    _client, _app, eng, mgr = _lane_app(tmp_path)
    for job_name, session_id in (("job-1", ""), ("job-2", None)):
        job_path, _job = _spool_job(tmp_path, eng, job_name, session_id)
        eng._ingest_drain_once()
        job_path.unlink(missing_ok=True)
    assert mgr.auto_snapshot_status() == {"failures": 0, "last_error": None, "last_error_at": None}
    assert _auto_snaps(mgr) == []


class _IngestOnlyEngine:
    """只有 ingest_status 的引擎替身（:memory: 没有 WorldManager）。"""
    def ingest_status(self) -> dict:
        return {"pending": 0, "processing": 0, "done": 0, "failed": 0, "ingest_remember_ms_total": 0}


def test_ingest_status_without_world_manager_reports_zero_failures():
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0),
                     engine=_IngestOnlyEngine())
    assert app.state.worlds is None
    body = TestClient(app).get("/api/ingest_status").json()
    assert body == {"pending": 0, "processing": 0, "done": 0, "failed": 0,
                    "ingest_remember_ms_total": 0, "auto_snapshot_failures": 0,
                    "auto_snapshot_last_error": None}


# ── lifespan ──

@pytest.mark.parametrize("shape", ["injected", "built_by_lifespan", "no_engine"])
def test_lifespan_attaches_the_hook_before_ingest_and_runs_the_scheduler(tmp_path, monkeypatch,
                                                                          shape):
    cfg = _cfg(tmp_path)
    cfg.tick_interval_s = 60.0 if shape == "built_by_lifespan" else 0
    hooks_at_start = []
    real_start = DashboardEngine.start_ingest_worker

    def _spy_start(self, *args, **kwargs):
        hooks_at_start.append(self.session_snapshot_hook)
        return real_start(self, *args, **kwargs)
    monkeypatch.setattr(DashboardEngine, "start_ingest_worker", _spy_start)
    app = create_app(cfg, engine=DashboardEngine(cfg) if shape == "injected" else None)
    before = set(_scheduler_threads())
    with TestClient(app):
        started = [t for t in _scheduler_threads() if t not in before]
        assert len(started) == 1, "调度线程在跑"
        engine = app.state.engine
        if shape == "no_engine":
            assert engine is None and hooks_at_start == []
        else:
            assert len(hooks_at_start) == 1 and hooks_at_start[0] is not None, \
                "摄入线程起来之前钩子就要挂上"
            assert engine.session_snapshot_hook is not None
        del engine
    assert all(not t.is_alive() for t in started), "退出 lifespan 之后调度线程已停"


def test_lifespan_without_world_manager_starts_no_scheduler():
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    before = set(_scheduler_threads())
    with TestClient(app):
        assert [t for t in _scheduler_threads() if t not in before] == []
