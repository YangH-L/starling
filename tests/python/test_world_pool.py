"""引擎池（Task 5）：多个世界同时开、按世界租约与关闭、恢复只停单个世界、设置继承与统计缓存。

设计：docs/superpowers/specs/2026-11-03-memory-service-design.md §5.5、§7.2、§8、§10.3
"""
import gc
import json
import threading
import time
import weakref

import pytest

from starling import _core
from starling.dashboard.config import DashboardConfig
from starling.dashboard.engine import DashboardEngine
from starling.dashboard.world_manager import (EngineLease, WorldContext, WorldPool,
                                              WorldSwitchError)

NOW = "2026-11-04T00:00:00Z"


def _stub(subject: str, obj: str) -> str:
    return json.dumps([{"holder": "self", "holder_perspective": "FIRST_PERSON",
                        "subject": subject, "predicate": "responsible_for", "object": obj,
                        "modality": "BELIEVES", "polarity": "POS", "nesting_depth": 0}])


def _fake(subject="Bob", obj="auth"):
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_stub(subject, obj), True, "")
    return fake


def _cfg(tmp_path, **over):
    base = dict(db_path=str(tmp_path / "dashboard.db"), token="", tick_interval_s=0,
                source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"))
    base.update(over)
    return DashboardConfig(**base)


def _registry(cfg):
    return _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)


def _pool(tmp_path, cfg=None, **kwargs):
    cfg = cfg or _cfg(tmp_path)
    reg = _registry(cfg)
    pool = WorldPool(reg, cfg, **kwargs)
    pool.sync_default_settings()
    return pool, reg, cfg


def _remember(pool, world, text, *, subject="Bob", obj="auth"):
    with pool.lease(world) as eng:
        eng.llm = _fake(subject, obj)
        return eng.remember(text)


def _recall_subjects(pool, world, query):
    with pool.lease(world) as eng:
        eng.tick(NOW)                      # 新语句要经 tick 嵌入才召回得到
        return {hit["row"].subject_id for hit in eng.recall(query)}


@pytest.fixture(autouse=True)
def _collect():
    yield
    gc.collect()


# ───────────────────────── 多世界同时开、互相隔离 ─────────────────────────

def test_two_worlds_open_at_once_and_isolated(tmp_path):
    pool, reg, _cfg_ = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice", obj="auth")
        _remember(pool, "w2", "Carol owns billing", subject="Carol", obj="billing")
        assert _recall_subjects(pool, "w1", "auth") == {"Alice"}
        assert _recall_subjects(pool, "w2", "billing") == {"Carol"}
        assert _recall_subjects(pool, "w1", "billing") <= {"Alice"}   # 召回不到 w2 的
        assert "Carol" not in _recall_subjects(pool, "w1", "Carol billing")
        assert "Alice" not in _recall_subjects(pool, "w2", "Alice auth")
        with pool.lease("default"):
            pass
        assert sorted(pool.open_worlds()) == ["default", "w1", "w2"]
        # 两个引擎各开自己的库文件，租户是世界名（C++ create_world 定的）。
        with pool.lease("w1") as e1, pool.lease("w2") as e2:
            assert e1 is not e2
            assert e1._db_path == reg.get("w1")["db_path"]
            assert e2._db_path == reg.get("w2")["db_path"]
            assert e1._core.tenant == "w1" and e2._core.tenant == "w2"
    finally:
        pool.close_all()


def test_lease_unknown_world_is_world_not_found(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("nope")
        assert ei.value.code == "world_not_found"
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("nope", build=False)
        assert ei.value.code == "world_not_found"
    finally:
        pool.close_all()


def test_lease_build_false_does_not_open_an_engine(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        with pool.lease("w1", build=False) as eng:
            assert eng is None
        assert "w1" not in pool.open_worlds()
        assert pool.live_stats("w1") is None
        with pool.lease("w1"):
            pass
        with pool.lease("w1", build=False) as eng:
            assert eng is not None                     # 开着就给已有的引擎
    finally:
        pool.close_all()


# ───────────────────────── Review Focus 1：并发首次打开只建一个引擎 ─────────────────────────

def test_concurrent_first_lease_builds_one_engine(tmp_path, monkeypatch):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        real_init = DashboardEngine.__init__
        built = []

        def slow_init(self, *args, **kwargs):
            built.append(threading.current_thread().name)
            time.sleep(0.2)                            # 撑开窗口：另一个线程此时也在 lease
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(DashboardEngine, "__init__", slow_init)
        barrier = threading.Barrier(2)
        got: dict = {}
        errors: list = []

        def worker(tag):
            try:
                barrier.wait()
                lease = pool.lease("w1")
                got[tag] = lease.engine
                time.sleep(0.05)
                lease.release()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert errors == []
        assert len(built) == 1
        assert got["a"] is got["b"] and got["a"] is not None
    finally:
        pool.close_all()


def test_concurrent_first_lease_of_different_worlds_builds_in_parallel(tmp_path, monkeypatch):
    """建引擎按世界持锁，不持池锁：w1 建引擎时 w2 的首次打开不排在它后面。"""
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        real_init = DashboardEngine.__init__

        def slow_init(self, ctx, *args, **kwargs):
            time.sleep(0.5)
            real_init(self, ctx, *args, **kwargs)

        monkeypatch.setattr(DashboardEngine, "__init__", slow_init)
        barrier = threading.Barrier(2)

        def worker(world):
            barrier.wait()
            pool.lease(world).release()

        start = time.monotonic()
        threads = [threading.Thread(target=worker, args=(w,)) for w in ("w1", "w2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert time.monotonic() - start < 0.9          # 串行的话至少 1.0 秒
        assert sorted(pool.open_worlds()) == ["w1", "w2"]
    finally:
        pool.close_all()


# ───────────────────────── Review Focus 2：关闭等在途租约 ─────────────────────────

def test_close_world_waits_for_in_flight_lease(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    # 回收宽限设为 0：关闭若不等租约，就会在持租约的请求还在写时立刻返回（宽限期会掩盖这一点）。
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.0)
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        first = pool.lease("w1")
        old_engine = first.engine
        old_engine.llm = _fake("Alice", "auth")
        old_rt = weakref.ref(old_engine._rt)
        old_engine = None
        released_at: dict = {}
        wrote: dict = {}

        def holder():
            time.sleep(0.5)
            wrote["out"] = first.engine.remember("Alice owns auth")   # 关闭等着的那个请求还在写
            released_at["t"] = time.monotonic()
            first.release()

        t = threading.Thread(target=holder)
        t.start()
        pool.close_world("w1")
        closed_at = time.monotonic()
        t.join(5)
        assert closed_at >= released_at["t"]
        assert wrote["out"]["outcome"] in ("accepted", "idempotent")
        assert "w1" not in pool.open_worlds()
        assert reg.get("w1")["statements"] == 1          # 关闭时写进统计缓存，含那次写入
        gc.collect()
        assert old_rt() is None                        # 旧 Runtime（C++ 写连接）已释放
        with pool.lease("w1") as eng:
            assert eng is not None and eng._rt is not None
            assert weakref.ref(eng._rt) is not old_rt  # 新对象
        assert "w1" in pool.open_worlds()
    finally:
        pool.close_all()


def test_lease_while_closing_returns_world_busy(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        held = pool.lease("w1")
        closer = threading.Thread(target=pool.close_world, args=("w1",))
        closer.start()
        deadline = time.monotonic() + 5
        while "w1" not in pool.closing_worlds() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "w1" in pool.closing_worlds()
        for build in (True, False):
            with pytest.raises(WorldSwitchError) as ei:
                pool.lease("w1", build=build)
            assert ei.value.code == "world_busy"
        with pool.lease("w2") as other:                # 别的世界不受影响
            assert other is not None
        held.release()
        closer.join(5)
        assert not closer.is_alive()
        assert pool.closing_worlds() == []
        with pool.lease("w1") as eng:                  # 关完之后照常重新打开
            assert eng is not None
    finally:
        pool.close_all()


def test_close_world_times_out_and_keeps_the_engine(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        held = pool.lease("w1")
        engine = held.engine
        with pytest.raises(WorldSwitchError) as ei:
            pool.close_world("w1", timeout_s=0.2)
        assert ei.value.code == "world_busy"
        assert pool.closing_worlds() == []             # 超时后标志清掉
        with pool.lease("w1") as again:
            assert again is engine                     # 引擎原样留着
        held.release()
        engine = again = None
        pool.close_world("w1")
        assert "w1" not in pool.open_worlds()
    finally:
        pool.close_all()


def test_default_world_is_not_closed_by_close_world(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        with pool.lease("default"):
            pass
        with pytest.raises(WorldSwitchError) as ei:
            pool.close_world("default")
        assert ei.value.code == "world_protected"
        assert "default" in pool.open_worlds()
        pool.close_all()
        assert pool.open_worlds() == []
    finally:
        pool.close_all()


# ───────────────────────── 恢复只停这一个世界 ─────────────────────────

def test_rollback_only_stops_that_world(tmp_path):
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "before")
        _remember(pool, "w1", "Dave owns ops", subject="Dave", obj="ops")
        assert _core.world_stats(reg.get("w1")["db_path"], "w1")["statements"] == 2
        with pool.lease("w2") as e2:
            w2_engine_id = id(e2)
            e2.llm = _fake("Carol", "billing")
        e2 = None
        stop = threading.Event()
        results: list = []
        errors: list = []

        def writer():
            idx = 0
            while not stop.is_set():
                try:
                    with pool.lease("w2") as eng:
                        results.append((id(eng), eng.remember(f"Carol owns billing {idx}")))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                idx += 1
                time.sleep(0.01)

        t = threading.Thread(target=writer)
        t.start()
        try:
            time.sleep(0.1)
            out = pool.rollback("w1", snap["id"])
            time.sleep(0.1)
        finally:
            stop.set()
            t.join(10)
        assert out["world"] == "w1" and out["snapshot_id"] == snap["id"]
        assert errors == []
        assert results and all(engine_id == w2_engine_id for engine_id, _r in results)
        assert all(r["outcome"] in ("accepted", "idempotent") for _e, r in results)
        assert _core.world_stats(reg.get("w1")["db_path"], "w1")["statements"] == 1
        assert "w1" in pool.open_worlds()             # 开着的世界恢复后重新打开
        with pool.lease("w1") as e1:
            assert e1.recall("ops") == [] or all(h["row"].subject_id != "Dave"
                                                 for h in e1.recall("ops"))
    finally:
        pool.close_all()


def test_rollback_waits_for_leases_and_refuses_when_busy(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.2)
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "")
        _remember(pool, "w1", "Dave owns ops", subject="Dave", obj="ops")
        pool.close_world("w1")                         # 关着：没有引擎、没有 Runtime 可钉
        held = pool.lease("w1", build=False)          # 只读检视（只读打开库文件）也挡恢复
        assert held.engine is None
        with pytest.raises(WorldSwitchError) as ei:
            pool.rollback("w1", snap["id"])
        assert ei.value.code == "world_busy"
        db = reg.get("w1")["db_path"]
        assert _core.world_stats(db, "w1")["statements"] == 2   # 库未动
        held.release()
        out = pool.rollback("w1", snap["id"])
        assert out["archived"] is True and out["reopened"] is False
        assert _core.world_stats(db, "w1")["statements"] == 1
    finally:
        pool.close_all()


def test_failed_close_does_not_pin_the_engine_in_the_exception(tmp_path):
    """关闭等租约超时抛 world_busy：调用方留着这个异常时，回溯里的栈帧不能钉住引擎，否则之后正常关闭也
    丢不掉它的 Runtime（同一个库上会留下第二条写连接）。"""
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        held = pool.lease("w1")
        rt = weakref.ref(held.engine._rt)
        try:
            pool.close_world("w1", timeout_s=0.1)
        except WorldSwitchError as exc:
            kept = exc                                 # 像路由那样留着异常
        assert kept.code == "world_busy"
        held.release()
        pool.close_world("w1")
        gc.collect()
        assert rt() is None
        del kept
    finally:
        pool.close_all()


def test_rollback_of_a_closed_world_does_not_open_it(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "")
        pool.close_world("w1")
        pool.rollback("w1", snap["id"])
        assert "w1" not in pool.open_worlds()
    finally:
        pool.close_all()


def test_rollback_refused_while_a_leaked_runtime_holds_the_db(tmp_path):
    """丢不掉的旧 Runtime 还开着写连接：恢复不能动这个库（world_busy），别的世界照常。"""
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "")
        with pool.lease("w1") as eng:
            pinned = eng._rt                          # 模拟别处还钉着旧 Runtime
        eng = None
        pool.close_world("w1")
        with pytest.raises(WorldSwitchError) as ei:
            pool.rollback("w1", snap["id"])
        assert ei.value.code == "world_busy"
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("w1")                          # 也不能再开第二条写连接
        assert ei.value.code == "world_busy"
        pinned = None
        gc.collect()
        pool.rollback("w1", snap["id"])               # 回收之后照常
        with pool.lease("w1") as eng2:
            assert eng2 is not None
    finally:
        pool.close_all()


# ───────────────────────── endpoint_changed ─────────────────────────

def test_inherited_endpoint_changed_refuses(tmp_path):
    cfg = _cfg(tmp_path, providers={"p": {"provider": "openai", "model": "m",
                                          "base_url": "http://a", "api_key": "k"}},
               roles={"extraction": "p"})

    def factory(ctx, providers):
        eng = DashboardEngine(ctx, providers)
        eng.llm = _fake()                              # 不发网络请求
        return eng

    pool, reg, _c = _pool(tmp_path, cfg=cfg, engine_factory=factory)
    try:
        rec = pool.create_world("w1", "")
        assert rec["settings"]["extraction"]["provider_name"] == "p"
        assert rec["settings"]["extraction"]["base_url"] == "http://a"
        assert _remember_raw(pool, "w1")["outcome"] in ("accepted", "idempotent")
        cfg.providers["p"]["base_url"] = "http://b"    # 改了 provider 的地址
        with pool.lease("w1") as eng:
            with pytest.raises(_core.WorldStoreError) as ei:
                eng.remember("Bob owns auth again")
        assert _core.world_store_error_code(ei.value) == "endpoint_changed"
        assert "k" not in str(ei.value)
        # 默认世界跟着 config 走（sync 之后同样记 b），新世界继承新地址。
        pool.sync_default_settings()
        assert reg.get("default")["settings"]["extraction"]["base_url"] == "http://b"
        rec2 = pool.create_world("w2", "")
        assert rec2["settings"]["extraction"]["base_url"] == "http://b"
        assert _remember_raw(pool, "w2")["outcome"] in ("accepted", "idempotent")
    finally:
        pool.close_all()


def _remember_raw(pool, world):
    with pool.lease(world) as eng:
        return eng.remember("Bob owns auth")


def test_world_without_endpoint_builds_unconfigured_adapter(tmp_path):
    """R15：存着的设置里没有抽取端点就不配抽取适配器，不回落到默认世界的端点。"""
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        assert "extraction" not in reg.get("w1")["settings"]
        with pool.lease("w1") as eng:
            assert eng.llm is None
            from starling.dashboard.engine import _LLMNotConfigured
            with pytest.raises(_LLMNotConfigured):
                eng.remember("Bob owns auth")
    finally:
        pool.close_all()


# ───────────────────────── 摄入分区 ─────────────────────────

def test_ingest_partitions_are_per_world(tmp_path):
    pool, reg, cfg = _pool(tmp_path)
    try:
        rec = pool.create_world("w1", "")
        root = cfg.ingest_spool_root()
        assert pool.context("default").ingest_spool_dir == str(root / "tenant-default")
        assert pool.context("w1").ingest_spool_dir == str(root / f"world-{rec['dir_id']}")
        with pool.lease("default") as d, pool.lease("w1") as w:
            assert d._ingest_spool == root / "tenant-default"
            assert w._ingest_spool == root / f"world-{rec['dir_id']}"
    finally:
        pool.close_all()


# ───────────────────────── 统计 ─────────────────────────

def test_engine_counts_track_writes_and_cache_on_close(tmp_path):
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        _remember(pool, "w1", "Carol owns billing", subject="Carol", obj="billing")
        live = pool.live_stats("w1")
        assert live["statements"] == 2 and live["holders"] == 1 and live["last_write_at"]
        assert reg.get("w1")["statements"] == 0        # 提交时不写注册表
        pool.close_world("w1")
        assert pool.live_stats("w1") is None
        got = reg.get("w1")
        assert got["statements"] == 2 and got["holders"] == 1
        assert got["last_write_at"] == live["last_write_at"]
    finally:
        pool.close_all()


def test_stats_refresh_writes_cache_for_open_worlds(tmp_path):
    pool, reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        pool.refresh_stats()
        assert reg.get("w1")["statements"] == 1
        assert reg.get("w1")["stats_at"]
    finally:
        pool.close_all()


def test_on_world_changed_is_debounced(tmp_path):
    calls: list = []
    pool, _reg, _c = _pool(tmp_path, on_world_changed=lambda w, s: calls.append((w, s)))
    try:
        pool.create_world("w1", "")
        with pool.lease("w1") as eng:
            eng.llm = _fake()
            for idx in range(5):
                eng.remember(f"Bob owns auth {idx}")
        eng = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if calls and calls[-1][1]["statements"] == 5:
                break
            time.sleep(0.05)
        time.sleep(1.2)                                # 尾沿之后不再多发
        assert 1 <= len(calls) <= 2
        assert all(w == "w1" for w, _s in calls)
        assert calls[-1][1]["statements"] == 5 and calls[-1][1]["last_write_at"]
        assert set(calls[-1][1]) == {"statements", "last_write_at"}
    finally:
        pool.close_all()


def test_on_world_changed_debounce_is_per_world(tmp_path):
    calls: list = []
    pool, _reg, _c = _pool(tmp_path, on_world_changed=lambda w, s: calls.append(w))
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        _remember(pool, "w1", "a")
        _remember(pool, "w2", "b")
        deadline = time.monotonic() + 3
        while set(calls) != {"w1", "w2"} and time.monotonic() < deadline:
            time.sleep(0.05)
        assert set(calls) == {"w1", "w2"}
    finally:
        pool.close_all()


# ───────────────────────── 设置：默认世界跟 config、新世界复制 ─────────────────────────

def test_default_settings_follow_config_and_new_worlds_copy_them(tmp_path):
    cfg = _cfg(tmp_path, providers={"p": {"provider": "openai", "model": "m1",
                                          "base_url": "http://a", "api_key": "secret"}},
               roles={"extraction": "p"})
    pool, reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        assert reg.get("default")["settings"]["extraction"]["model"] == "m1"
        pool.create_world("w1", "")
        cfg.providers["p"]["model"] = "m2"
        pool.sync_default_settings()
        assert reg.get("default")["settings"]["extraction"]["model"] == "m2"
        assert reg.get("w1")["settings"]["extraction"]["model"] == "m1"
        pool.create_world("w2", "")
        assert reg.get("w2")["settings"]["extraction"]["model"] == "m2"
        # 密钥不进注册表。
        for name in ("default", "w1", "w2"):
            assert "secret" not in json.dumps(reg.get(name)["settings"])
    finally:
        pool.close_all()


def test_sync_default_settings_keeps_stored_auto_snapshot(tmp_path):
    """R16：config.world_settings() 不带 auto_snapshot，同步不覆盖界面上改过的值。"""
    pool, reg, cfg = _pool(tmp_path)
    try:
        assert "auto_snapshot" not in cfg.world_settings().get("background", {})
        pool.set_auto_snapshot("default", True, 6)
        pool.sync_default_settings()
        assert reg.get("default")["settings"]["background"]["auto_snapshot"] == {
            "on_session": True, "interval_hours": 6}
    finally:
        pool.close_all()


def test_context_carries_effective_settings(tmp_path):
    cfg = _cfg(tmp_path, tick_interval_s=12.0, gist_thresholds={"min_holders": 4})
    pool, _reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        ctx = pool.context("default")
        assert isinstance(ctx, WorldContext) and ctx.is_default and ctx.name == "default"
        assert ctx.settings["background"]["tick_interval_s"] == 12.0
        assert ctx.settings["gist_thresholds"] == {"min_holders": 4.0}
        assert ctx.settings["source_lane"]["enabled"] is False
        with pool.lease("default") as eng:
            # 设置里阈值存成 double；计数类阈值要按整数注入，否则 ReplayScheduler 的绑定拒收。
            assert eng._core.gist_thresholds == {"min_holders": 4}
            assert type(eng._core.gist_thresholds["min_holders"]) is int
            assert eng.run_replay("idle")["mode"] == "idle"
    finally:
        pool.close_all()


# ───────────────────────── 后台线程按世界起停 ─────────────────────────

def test_start_background_follows_each_world_settings(tmp_path):
    pool, reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    try:
        pool.create_world("w1", "", overrides={"background": {"tick_interval_s": 0,
                                                              "ingest": False}})
        with pool.lease("default"), pool.lease("w1"):
            pass
        pool.start_background()
        with pool.lease("default") as d, pool.lease("w1") as w:
            assert d._tick_thread is not None and d._ingest_thread is not None
            assert w._tick_thread is None and w._ingest_thread is None
            assert d.session_snapshot_hook is not None
        pool.create_world("w2", "")
        with pool.lease("w2") as w2:                   # 后台开着之后才建的引擎也起线程
            assert w2._tick_thread is not None and w2._ingest_thread is not None
        pool.stop_background()
        with pool.lease("default") as d:
            assert d._tick_thread is None and d._ingest_thread is None
    finally:
        pool.close_all()


def test_close_all_stops_threads_and_releases_runtimes(tmp_path):
    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    pool.create_world("w1", "")
    refs = []
    for world in ("default", "w1"):
        with pool.lease(world) as eng:
            refs.append(weakref.ref(eng._rt))
    eng = None
    pool.start_background()
    pool.close_all()
    gc.collect()
    assert pool.open_worlds() == []
    assert all(ref() is None for ref in refs)
    with pytest.raises(WorldSwitchError) as ei:
        pool.lease("w1")
    assert ei.value.code == "world_busy"


# ───────────────────────── 管理操作 ─────────────────────────

def test_delete_world_closes_it_first(tmp_path):
    pool, reg, _c = _pool(tmp_path)
    try:
        rec = pool.create_world("w1", "")
        with pool.lease("w1") as eng:
            rt = weakref.ref(eng._rt)
        eng = None
        pool.delete_world("w1")
        gc.collect()
        assert rt() is None
        assert "w1" not in pool.open_worlds()
        with pytest.raises(_core.WorldStoreError):
            reg.get("w1")
        import os
        assert not os.path.exists(os.path.dirname(rec["db_path"]))
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("w1")
        assert ei.value.code == "world_not_found"
    finally:
        pool.close_all()


def test_delete_default_world_is_protected(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        with pool.lease("default"):
            pass
        with pytest.raises((_core.WorldStoreError, WorldSwitchError)) as ei:
            pool.delete_world("default")
        code = getattr(ei.value, "code", None) or _core.world_store_error_code(ei.value)
        assert code == "world_protected"
        assert "default" in pool.open_worlds()
    finally:
        pool.close_all()


def test_snapshot_listing_and_management_take_the_world_name(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "n1")
        assert [s["id"] for s in pool.snapshots("w1")] == [snap["id"]]
        assert pool.snapshots("default") == []
        sessions = pool.list_sessions("w1", 10)
        assert sessions["total"] == 0
        pool.rollback("w1", snap["id"])
        assert len(pool.archives("w1")) == 1
        assert pool.purge_archives("w1") == 1
        pool.delete_snapshot("w1", snap["id"])
        assert pool.snapshots("w1") == []
        assert pool.set_auto_snapshot("w1", False, 12) == {"on_session": False,
                                                            "interval_hours": 12}
        assert pool.auto_snapshot_status() == {"failures": 0, "last_error": None,
                                               "last_error_at": None}
    finally:
        pool.close_all()


def test_interval_scheduler_round_visits_every_world(tmp_path):
    clock = {"now": "2026-11-04T00:00:00Z"}
    pool, reg, _c = _pool(tmp_path, clock=lambda: clock["now"])
    try:
        pool.create_world("w1", "")
        pool.create_world("w2", "")
        pool.set_auto_snapshot("w1", False, 1)
        pool.set_auto_snapshot("w2", False, 1)
        pool.set_auto_snapshot("default", False, 0)
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        _remember(pool, "w2", "Carol owns billing", subject="Carol", obj="billing")
        pool.close_world("w2")                         # 关着的世界同样按时间拍
        out = pool.auto_snapshot_round()
        assert out["w1"].startswith("taken:") and out["w2"].startswith("taken:")
        assert out["default"] == "skipped:disabled"
        assert "w2" not in pool.open_worlds()           # 拍快照不打开世界
        assert len(reg.snapshots("w1")) == 1 and len(reg.snapshots("w2")) == 1
    finally:
        pool.close_all()


def test_world_management_methods_need_a_registry(tmp_path):
    """R6：:memory: 库没有注册表，池只服务默认世界，管理方法一律 worlds_unavailable。"""
    cfg = _cfg(tmp_path, db_path=":memory:")
    pool = WorldPool(None, cfg)
    try:
        assert pool.registry is None
        with pool.lease("default") as eng:
            assert eng is not None
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("w1")
        assert ei.value.code == "world_not_found"
        for call in (lambda: pool.create_world("w1", ""),
                     lambda: pool.take_snapshot("default", ""),
                     lambda: pool.rollback("default", "x"),
                     lambda: pool.delete_world("w1"),
                     lambda: pool.snapshots("default"),
                     lambda: pool.sync_default_settings()):
            with pytest.raises(WorldSwitchError) as ei:
                call()
            assert ei.value.code == "worlds_unavailable"
        assert pool.live_stats("default")["statements"] == 0
    finally:
        pool.close_all()


def test_lease_is_an_engine_lease_and_release_is_idempotent(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        lease = pool.lease("default")
        assert isinstance(lease, EngineLease)
        lease.release()
        lease.release()
        pool.close_all()
    finally:
        pool.close_all()


def test_data_dir_follows_env_or_db_parent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    assert cfg.data_dir == str(tmp_path / "data")      # conftest 设了 STARLING_DATA_DIR
    monkeypatch.delenv("STARLING_DATA_DIR")
    assert cfg.data_dir == str(tmp_path)


# ───────────────────────── 修复轮 1 ─────────────────────────

def test_failed_worker_start_publishes_the_engine_and_retries(tmp_path, monkeypatch):
    """裁定 R23：起后台线程失败时引擎照样发布（标记 workers-lost），下一次租约补起线程。不能留一个没登记的
    活 Runtime，让下一次租约在同一个库上再建一条写连接。"""
    refs = []

    def factory(ctx, providers):
        eng = DashboardEngine(ctx, providers)
        refs.append(weakref.ref(eng._rt))
        return eng

    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0),
                           engine_factory=factory)
    try:
        pool.create_world("w1", "")
        pool.start_background(stats_interval_s=999, scheduler_poll_s=999,
                              scheduler_first_delay_s=999)
        real = DashboardEngine.start_ingest_worker
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("spool unreadable")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(DashboardEngine, "start_ingest_worker", flaky)
        with pool.lease("w1") as first:                      # 起线程失败不让租约失败
            assert first is not None
            first_id = id(first)
        first = None
        assert "w1" in pool.open_worlds()
        assert pool.workers_lost_worlds() == ["w1"]
        with pool.lease("w1") as second:                     # 下一次租约补起线程
            assert id(second) == first_id
            assert second._tick_thread is not None and second._ingest_thread is not None
        second = None
        gc.collect()
        assert sum(ref() is not None for ref in refs) == 1    # 只有一个 Runtime 活着
        assert pool.workers_lost_worlds() == []
        ticks = [t for t in threading.enumerate() if t.name == "starling-bg-tick"]
        assert len(ticks) == 1
    finally:
        pool.close_all()


def test_reopen_after_rollback_with_failed_workers_still_publishes(tmp_path, monkeypatch):
    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    try:
        pool.create_world("w1", "")
        _remember(pool, "w1", "Alice owns auth", subject="Alice")
        snap = pool.take_snapshot("w1", "")
        pool.start_background(stats_interval_s=999, scheduler_poll_s=999,
                              scheduler_first_delay_s=999)

        def boom(self, *args, **kwargs):
            raise OSError("spool unreadable")

        monkeypatch.setattr(DashboardEngine, "start_ingest_worker", boom)
        out = pool.rollback("w1", snap["id"])                # 恢复本身成功
        assert out["reopened"] is True
        assert "w1" in pool.open_worlds() and pool.workers_lost_worlds() == ["w1"]
    finally:
        pool.close_all()


def test_unbinding_a_role_and_resetting_gist_reach_the_registry(tmp_path):
    """裁定 R24（复现审查的两个场景）：解绑抽取角色后同步，重建的默认世界引擎没有 llm；gist 阈值设成 {}
    后同步，注册表里也是 {}。"""
    cfg = _cfg(tmp_path, providers={"p": {"provider": "openai", "model": "m",
                                          "base_url": "http://a", "api_key": "k"}},
               roles={"extraction": "p"}, gist_thresholds={"min_holders": 4})
    pool, reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        assert reg.get("default")["settings"]["extraction"]["provider_name"] == "p"
        cfg.roles["extraction"] = ""
        cfg.gist_thresholds = {}
        pool.sync_default_settings()
        stored = reg.get("default")["settings"]
        assert "extraction" not in stored and "chat" not in stored
        assert stored["gist_thresholds"] == {}
        eng = DashboardEngine(pool.context("default"), cfg.providers)
        assert eng.llm is None
        eng = None
        pool.create_world("w2", "")                            # 新世界也不继承旧绑定
        assert "extraction" not in reg.get("w2")["settings"]
    finally:
        pool.close_all()


def test_pool_built_default_world_has_the_consolidation_role(tmp_path, monkeypatch):
    """裁定 R25：按 pool.context("default") 建的默认世界引擎有 consolidation 适配器。"""
    import starling.dashboard.engine as engmod
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda cfg: _fake())
    cfg = _cfg(tmp_path, providers={"j": {"provider": "openai", "model": "judge",
                                          "base_url": "http://j", "api_key": "k"}},
               roles={"consolidation": "j"})
    pool, reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        assert reg.get("default")["settings"]["consolidation"]["model"] == "judge"
        with pool.lease("default") as eng:
            assert eng._cfg is None                            # 走的是 WorldContext 这条路
            assert eng._core.consolidation_llm is not None
        cfg.roles["consolidation"] = ""
        pool.sync_default_settings()
        assert "consolidation" not in reg.get("default")["settings"]
    finally:
        pool.close_all()


def test_registry_default_world_uses_config_vector_backend(tmp_path, monkeypatch, caplog):
    """裁定 R26：注册表的默认世界按 config 的向量后端与路径建引擎（默认世界「原地」）；注册表里的设置与新世界
    继承到的都是 sqlite；config 是 zvec 时同步记一次 warning。"""
    import starling._memory_core as mc
    seen = []

    def spy(backend, dim, store_path):
        seen.append((backend, store_path))
        return _core.SqliteBlobVectorIndex()

    monkeypatch.setattr(mc, "_make_vector_index", spy)
    cfg = _cfg(tmp_path, vector_backend="zvec", vector_store_path=str(tmp_path / "vec"))
    with caplog.at_level("WARNING"):
        pool, reg, _c = _pool(tmp_path, cfg=cfg)
        pool.sync_default_settings()
    try:
        assert sum("zvec" in rec.message for rec in caplog.records) == 1   # 只记一次
        assert cfg.world_settings()["vector_backend"] == "sqlite"
        assert reg.get("default")["settings"]["vector_backend"] == "sqlite"
        with pool.lease("default"):
            pass
        assert seen[-1] == ("zvec", str(tmp_path / "vec"))
        pool.create_world("w1", "")
        assert reg.get("w1")["settings"]["vector_backend"] == "sqlite"
        with pool.lease("w1"):
            pass
        assert seen[-1] == ("sqlite", None)
    finally:
        pool.close_all()


# ───────────────────────── 修复轮 2 ─────────────────────────

def test_rebinding_a_role_leaves_no_old_provider_leaves(tmp_path, monkeypatch):
    """裁定 R27（复现 bleed.py）：抽取角色从 a 改绑到 b 之后同步，存着的 endpoint 与 b 的字段完全一致，不留
    a 的 thinking_type / max_tokens / max_retries / enable_thinking；gist 少了的键同步后也没了；新世界继承
    的是 b；按 pool.context 建的引擎拿 b 的字段建适配器。"""
    import starling.dashboard.engine as engmod
    cfg = _cfg(tmp_path,
               providers={"a": {"provider": "openai", "model": "ma", "base_url": "http://a",
                                "api_key": "ka", "thinking_type": "disabled", "max_tokens": 8192,
                                "max_retries": 5, "enable_thinking": False},
                          "b": {"provider": "openai", "model": "mb", "base_url": "http://b",
                                "api_key": "kb"}},
               roles={"extraction": "a"},
               gist_thresholds={"min_holders": 4, "min_confidence": 0.7})
    pool, reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        cfg.roles["extraction"] = "b"
        cfg.gist_thresholds = {"min_holders": 4}
        pool.sync_default_settings()
        fresh = _core.world_effective_settings(
            {"extraction": {"provider": "openai", "provider_name": "b", "model": "mb",
                            "base_url": "http://b"}})["extraction"]
        stored = reg.get("default")["settings"]
        assert stored["extraction"] == fresh
        assert stored["gist_thresholds"] == {"min_holders": 4.0}
        pool.create_world("w1", "")
        assert reg.get("w1")["settings"]["extraction"] == fresh
        seen = []
        monkeypatch.setattr(engmod, "_build_chat_adapter",
                            lambda c: seen.append(dict(c)) or _core.FakeLLMAdapter())
        eng = DashboardEngine(pool.context("default"), cfg.providers)
        got = {k: v for k, v in seen[0].items() if k != "api_key"}
        assert got == {k: fresh[k] for k in got}
        assert "max_retries" not in got and "enable_thinking" not in got
        assert got["thinking_type"] == fresh["thinking_type"] != "disabled"
        eng = None
    finally:
        pool.close_all()


def test_rebinding_to_a_provider_without_base_url_does_not_brick_the_default_world(tmp_path,
                                                                                   monkeypatch):
    """裁定 R27（复现 bleed2.py）：改绑到没有 base_url 的 provider b 之后，存着的不能是「b 的名字 + a 的地址」，
    否则默认世界的 remember 永远 endpoint_changed。"""
    import starling.dashboard.engine as engmod
    cfg = _cfg(tmp_path,
               providers={"a": {"provider": "openai", "model": "ma", "base_url": "http://a",
                                "api_key": "ka"},
                          "b": {"provider": "openai", "model": "mb", "api_key": "kb"}},
               roles={"extraction": "a"})
    pool, reg, _c = _pool(tmp_path, cfg=cfg)
    try:
        cfg.roles["extraction"] = "b"
        pool.sync_default_settings()
        ext = reg.get("default")["settings"]["extraction"]
        assert (ext["provider_name"], ext["base_url"], ext["model"]) == ("b", "", "mb")
        monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: _fake())
        eng = DashboardEngine(pool.context("default"), cfg.providers)   # 重启后建的就是这个
        assert eng.remember("Bob owns auth")["outcome"] in ("accepted", "idempotent")
        eng = None
    finally:
        pool.close_all()


def test_sync_keeps_stored_auto_snapshot_and_ingest_after_the_replace(tmp_path):
    """R27 下 background 仍逐叶合并：config 不带 auto_snapshot、ingest，同步不动它们（R16）。"""
    pool, reg, cfg = _pool(tmp_path)
    try:
        pool.set_auto_snapshot("default", True, 6)
        reg.update_settings("default", {"background": {"ingest": False}})
        cfg.tick_interval_s = 12.0
        pool.sync_default_settings()
        bg = reg.get("default")["settings"]["background"]
        assert bg == {"tick_interval_s": 12.0, "ingest": False,
                      "auto_snapshot": {"on_session": True, "interval_hours": 6}}
    finally:
        pool.close_all()


def test_worker_start_failure_logging_does_not_pin_the_engine(tmp_path, monkeypatch):
    """修复轮 2 小项 1：起线程失败的日志不能把带引擎栈帧的回溯留在 LogRecord 里。挂一个留着记录的 handler
    （与 caplog、队列 handler 一样），关闭之后要能证明 Runtime 已回收，重开照常，_leaked 为空。"""
    import logging
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.2)

    class Keep(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    keep = Keep()
    wm.logger.addHandler(keep)
    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    try:
        pool.create_world("w1", "")
        pool.start_background(stats_interval_s=999, scheduler_poll_s=999,
                              scheduler_first_delay_s=999)

        def boom(self, *args, **kwargs):
            raise OSError("spool unreadable")

        monkeypatch.setattr(DashboardEngine, "start_ingest_worker", boom)
        with pool.lease("w1") as eng:
            rt = weakref.ref(eng._rt)
        eng = None
        assert any("spool unreadable" in rec.getMessage() for rec in keep.records)
        assert all(rec.exc_info is None for rec in keep.records)
        monkeypatch.undo()
        monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.2)
        pool.close_world("w1")
        gc.collect()
        assert rt() is None
        assert pool._leaked == []
        with pool.lease("w1") as again:
            assert again is not None
    finally:
        wm.logger.removeHandler(keep)
        pool.close_all()


# ───────────────────────── 注入引擎（create_app(engine=...)，Task 6） ─────────────────────────

def test_seed_engine_publishes_an_injected_engine_without_starting_threads(tmp_path):
    """seed_engine 把建好的引擎放进池：租约拿到的就是它，池的回调已挂上，不起线程；
    start_background 之后按世界设置起线程；同一世界不能再塞第二个。"""
    calls = []
    cfg = _cfg(tmp_path, tick_interval_s=60.0)
    pool, _reg, _c = _pool(tmp_path, cfg=cfg, on_world_changed=lambda w, s: calls.append(w))
    try:
        injected = DashboardEngine(cfg)
        pool.seed_engine("default", injected)
        assert pool.open_worlds() == ["default"]
        assert injected._tick_thread is None and injected._ingest_thread is None
        assert injected.session_snapshot_hook is not None
        with pool.lease("default") as eng:
            assert eng is injected
            eng.llm = _fake()
            eng.remember("Bob owns auth")
        eng = None
        assert calls == ["default"]                      # on_commit 已接到池的去抖广播
        with pytest.raises(WorldSwitchError) as ei:
            pool.seed_engine("default", injected)
        assert ei.value.code == "world_busy"
        pool.start_background(stats_interval_s=999, scheduler_poll_s=999,
                              scheduler_first_delay_s=999)
        assert injected._tick_thread is not None and injected._tick_thread.is_alive()
        assert injected._ingest_thread is not None and injected._ingest_thread.is_alive()
        threads = (injected._tick_thread, injected._ingest_thread)
        injected = None
        pool.close_all()
        assert not any(t.is_alive() for t in threads)
        assert pool._leaked == []
    finally:
        pool.close_all()


def test_seed_engine_refuses_unknown_world_and_shut_pool(tmp_path):
    pool, _reg, cfg = _pool(tmp_path)
    injected = DashboardEngine(cfg)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            pool.seed_engine("nope", injected)
        assert ei.value.code == "world_not_found"
        pool.close_all()
        with pytest.raises(WorldSwitchError) as ei:
            pool.seed_engine("default", injected)
        assert ei.value.code == "world_busy"
        assert pool.open_worlds() == []
    finally:
        injected.close()
        pool.close_all()


def test_begin_drain_puts_every_open_world_into_draining(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        for world in ("default", "w1"):
            with pool.lease(world):
                pass
        pool.begin_drain()
        for world in ("default", "w1"):
            with pool.lease(world, build=False) as eng:
                assert eng.health() == _core.RuntimeHealth.DRAINING
            eng = None
    finally:
        pool.close_all()


def test_a_first_open_that_finishes_after_shutdown_is_not_published(tmp_path, monkeypatch):
    """close_all 只关它列出那一刻表上的世界。一个正卡在首次打开里（已持世界锁、还没发布）的世界不在表上：
    它在关停之后才发布的话，写连接一直开着。发布时看到池已关停就不放进表、关掉这个引擎，租约回 world_busy。"""
    pool, _reg, _c = _pool(tmp_path)
    pool.create_world("w1", "")
    real = pool._open_engine
    in_build, proceed = threading.Event(), threading.Event()
    built: list = []

    def _slow_open(world, ctx):
        engine = real(world, ctx)
        built.append(weakref.ref(engine._rt))
        in_build.set()
        assert proceed.wait(15)
        return engine
    monkeypatch.setattr(pool, "_open_engine", _slow_open)
    errors: list = []

    def _first_open():
        try:
            pool.lease("w1").release()
        except WorldSwitchError as exc:
            errors.append(exc.code)

    opener = threading.Thread(target=_first_open)
    opener.start()
    try:
        assert in_build.wait(10)
        pool.close_all(timeout_s=0.2)               # 首次打开的租约还没归还：等满超时，照常关停
    finally:
        proceed.set()
        opener.join(15)
    assert errors == ["world_busy"]
    assert pool.open_worlds() == []
    assert pool._leaked == [], "丢弃的引擎没能证明已回收（本帧还钉着它）"
    gc.collect()
    assert built and built[0]() is None, "关停之后建好的引擎没关，它的写连接还开着"


def test_stop_background_waits_for_a_first_open_that_is_starting_its_workers(tmp_path,
                                                                           monkeypatch):
    """首次打开已把引擎放进表、读到后台开着、正要起线程时开始关后台：stop_background 等它起完（世界锁），
    停掉的正是它刚起的线程，返回之后没有线程还在跑。卡点用 Event。"""
    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    pool.create_world("w1", "")
    pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                          scheduler_first_delay_s=3600.0)
    real = pool._start_workers
    in_start, proceed = threading.Event(), threading.Event()
    started: list = []

    def _slow_start(world, engine):
        if world == "w1":
            in_start.set()
            assert proceed.wait(15)
        real(world, engine)
        started.append((engine._tick_thread, engine._ingest_thread))
    monkeypatch.setattr(pool, "_start_workers", _slow_start)
    opener = threading.Thread(target=lambda: pool.lease("w1").release())
    stopper = None
    try:
        opener.start()
        assert in_start.wait(10)
        stopper = threading.Thread(target=pool.stop_background)
        stopper.start()
        stopper.join(0.3)
        assert stopper.is_alive(), "stop_background 没等正在起线程的首次打开"
        proceed.set()
        opener.join(15)
        stopper.join(15)
        assert not stopper.is_alive()
        assert started and all(t is not None for t in started[0])
        assert not any(t.is_alive() for t in started[0]), "关后台之后线程还在跑"
    finally:
        proceed.set()
        opener.join(15)
        if stopper is not None:
            stopper.join(15)
        pool.close_all()


def test_rollback_reports_reopened_from_the_actual_outcome(tmp_path, monkeypatch):
    """恢复途中池关停了：重建好的引擎不发布、关掉，世界关着。返回的 reopened 按实际结果是 False，而不是按恢复前
    开着就报 True；丢弃的引擎证明已回收，不进 _leaked。"""
    pool, _reg, _c = _pool(tmp_path)
    pool.create_world("w1", "")
    _remember(pool, "w1", "Alice owns auth", subject="Alice")
    snap = pool.take_snapshot("w1", "")
    with pool.lease("w1"):
        pass
    real = pool._open_engine

    def _open_then_shut(world, ctx):
        engine = real(world, ctx)
        with pool._lock:
            pool._shut = True                      # 重建期间池关停（close_all 置的标志）
        return engine
    monkeypatch.setattr(pool, "_open_engine", _open_then_shut)
    out = pool.rollback("w1", snap["id"])
    assert out["reopened"] is False and out["archived"] is True
    assert pool.open_worlds() == [] and pool._leaked == []
    pool.close_all()


def test_update_settings_of_an_unknown_world_creates_no_world_lock(tmp_path):
    pool, _reg, _c = _pool(tmp_path)
    try:
        with pytest.raises(WorldSwitchError) as ei:
            pool.update_settings("nope", {})
        assert ei.value.code == "world_not_found"
        assert "nope" not in pool._world_locks
    finally:
        pool.close_all()


def test_stop_background_stops_the_scheduler_before_taking_any_world_lock(tmp_path, monkeypatch):
    """stop_background 先停调度与统计线程、join 它们，再逐个世界取世界锁停引擎线程。反过来的话，调度线程正卡在
    某个世界的自动快照里（持着它的世界锁）时，关后台会在世界锁上等，而调度线程自己又在等关后台放行。
    记下事件顺序：调度线程退出在先，第一次取世界锁在后。"""
    pool, _reg, _c = _pool(tmp_path, cfg=_cfg(tmp_path, tick_interval_s=60.0))
    events: list = []
    real_loop = pool._scheduler_loop

    def _tracked_loop(stop, poll_s, first_delay_s):
        try:
            real_loop(stop, poll_s, first_delay_s)
        finally:
            events.append("scheduler_exited")
    monkeypatch.setattr(pool, "_scheduler_loop", _tracked_loop)
    with pool.lease("default"):
        pass
    pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                          scheduler_first_delay_s=3600.0)
    real_lock = pool._world_lock

    def _tracked_lock(world):
        events.append(f"world_lock:{world}")
        return real_lock(world)
    monkeypatch.setattr(pool, "_world_lock", _tracked_lock)
    try:
        pool.stop_background()
        assert "scheduler_exited" in events and "world_lock:default" in events, events
        assert events.index("scheduler_exited") < events.index("world_lock:default"), events
    finally:
        monkeypatch.undo()
        pool.close_all()


def test_set_auto_snapshot_serializes_with_update_settings(tmp_path, monkeypatch):
    """裁定 R42：set_auto_snapshot 持世界锁，与 update_settings 串行：update_settings 写完注册表、还没把新设置
    交给引擎时，set_auto_snapshot 不能插进来（插进来就会先把合并后的设置换上引擎，apply_world_settings 随后
    看不出 endpoint 变了、不重建适配器）。卡点：update_settings 调 apply_world_settings 时停住。"""
    pool, _reg, _c = _pool(tmp_path)
    try:
        pool.create_world("w1", "")
        with pool.lease("w1"):
            pass
        from starling.dashboard.engine import DashboardEngine
        in_apply, proceed = threading.Event(), threading.Event()
        real_apply = DashboardEngine.apply_world_settings

        def _paused_apply(self, settings):
            in_apply.set()
            assert proceed.wait(15)
            return real_apply(self, settings)
        monkeypatch.setattr(DashboardEngine, "apply_world_settings", _paused_apply)
        updater = threading.Thread(
            target=lambda: pool.update_settings("w1", {"gist_thresholds": {"min_holders": 4}}))
        updater.start()
        assert in_apply.wait(10)
        done = threading.Event()
        setter = threading.Thread(target=lambda: (pool.set_auto_snapshot("w1", True, 6), done.set()))
        setter.start()
        try:
            assert not done.wait(0.3), "set_auto_snapshot 没等 update_settings 交完设置"
        finally:
            proceed.set()
        updater.join(10)
        assert done.wait(10)
        setter.join(5)
        with pool.lease("w1", build=False) as eng:
            assert eng._settings["background"]["auto_snapshot"] == {"on_session": True,
                                                                    "interval_hours": 6}
            assert eng._settings["gist_thresholds"] == {"min_holders": 4.0}
        eng = None
    finally:
        pool.close_all()
