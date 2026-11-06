"""看板 API 按世界寻址（记忆服务第 1 期 Task 6）：/api/w/{world}/... 与旧路径（默认世界）。

建世界的 HTTP 路由（POST /api/worlds）、列表、设置、关闭的用例属于 Task 6b；这里的用例直接经引擎池
建世界、关世界，只测检视与命令路由的寻址和广播。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from starling import _core, make_stub_llm
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine

_STUB = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
         '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
         '"polarity":"POS","nesting_depth":0}]')


def _stub_factory(ctx, providers):
    """池的引擎工厂：照常建引擎，再换上离线的抽取适配器（不发网络请求）。"""
    engine = DashboardEngine(ctx, providers)
    engine.llm = make_stub_llm(default_response=_STUB)
    return engine


@pytest.fixture
def client(tmp_path):
    # tick_interval_s > 0：lifespan 照生产形态即建默认世界的引擎（库与 schema 从启动起就在）。
    # 600 秒的 tick 不会在测试窗口内触发。
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    with TestClient(create_app(cfg, engine_factory=_stub_factory)) as test_client:
        yield test_client


def _pool(client):
    return client.app.state.pool


def _remember(client, prefix: str, text: str = "Bob owns auth") -> dict:
    resp = client.post(f"{prefix}/remember", json={"text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _statement_ids(client, prefix: str) -> list[str]:
    resp = client.get(f"{prefix}/statements")
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["rows"]]


def test_inspect_routes_by_world(client):
    pool = _pool(client)
    pool.create_world("w1", "")
    written = _remember(client, "/api/w/w1")
    assert _statement_ids(client, "/api/w/w1") == written["statement_ids"]
    assert _statement_ids(client, "/api") == []                  # 默认世界没被写
    assert client.get("/api/w/w1/overview").json()["counts"]["statements"] == 1
    assert client.get("/api/overview").json()["counts"]["statements"] == 0

    # 检视关着的世界：只读打开它的库，不建引擎。
    pool.create_world("w2", "")
    other = _remember(client, "/api/w/w2", "Carol owns billing")
    pool.close_world("w2")
    assert "w2" not in pool.open_worlds()
    assert _statement_ids(client, "/api/w/w2") == other["statement_ids"]
    assert "w2" not in pool.open_worlds()
    # 读引擎内存态的路由对关着的世界照旧 503（没有引擎可读），同样不建引擎。
    assert client.get("/api/w/w2/runtime_health").status_code == 503
    assert "w2" not in pool.open_worlds()


def test_legacy_paths_hit_default_world(client):
    pool = _pool(client)
    assert pool.default_world() == "default"
    written = _remember(client, "/api")                          # 旧路径写默认世界
    assert _statement_ids(client, "/api") == written["statement_ids"]
    assert _statement_ids(client, "/api/w/default") == written["statement_ids"]
    more = _remember(client, "/api/w/default", "Carol owns billing")
    assert set(_statement_ids(client, "/api")) == set(written["statement_ids"]
                                                       + more["statement_ids"])
    legacy = client.get("/api/runtime_health")
    explicit = client.get("/api/w/default/runtime_health")
    assert legacy.status_code == explicit.status_code == 200
    assert legacy.json()["status"] == explicit.json()["status"] == "READY"
    assert pool.open_worlds() == ["default"]


def test_unknown_world_404(client):
    pool = _pool(client)
    for method, path, body in (("get", "/api/w/nope/statements", None),
                               ("get", "/api/w/nope/overview", None),
                               ("get", "/api/w/nope/runtime_health", None),
                               ("post", "/api/w/nope/remember", {"text": "x"}),
                               ("post", "/api/w/nope/tick", {})):
        resp = client.request(method, path, json=body)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json() == {"detail": "world_not_found"}
    assert "nope" not in pool.open_worlds()


def test_ws_broadcast_carries_world(client):
    pool = _pool(client)
    pool.create_world("w1", "")
    with client.websocket_connect("/ws") as ws:
        written = _remember(client, "/api/w/w1")
        # 哨兵帧排在这两条广播之后：读到它就说明前面该到的都到了，读不到也不会一直等。
        client.portal.call(client.app.state.ws_manager.broadcast, {"type": "sentinel"})
        frames = []
        while (msg := ws.receive_json())["type"] != "sentinel":
            frames.append(msg)
    changed = [f for f in frames if f["type"] == "world_changed"]
    assert changed, frames
    assert changed[0]["world"] == "w1"
    assert changed[0]["payload"]["statements"] == 1
    assert set(changed[0]["payload"]) == {"statements", "last_write_at"}
    added = [f for f in frames if f["type"] == "statement_added"]
    assert added and added[0]["world"] == "w1"
    assert added[0]["payload"] == {"statement_ids": written["statement_ids"]}


# ───────────────────────── 状态码映射与其余寻址入口 ─────────────────────────

def test_world_errors_map_to_http_codes(client, monkeypatch):
    """world_busy → 503；引擎调用里的 endpoint_changed（C++ WorldStoreError）→ 409。body 一律
    {"detail": 码}。"""
    pool = _pool(client)
    pool.create_world("w1", "")
    with pool._lock:
        pool._closing.add("w1")                         # 关闭窗口内的新租约一律 world_busy
    try:
        for path in ("/api/w/w1/statements", "/api/w/w1/runtime_health"):
            resp = client.get(path)
            assert (resp.status_code, resp.json()) == (503, {"detail": "world_busy"}), path
        resp = client.post("/api/w/w1/remember", json={"text": "x"})
        assert (resp.status_code, resp.json()) == (503, {"detail": "world_busy"})
    finally:
        with pool._lock:
            pool._closing.discard("w1")

    from starling import _core

    def changed(self, role, *_snapshot):
        _core.world_require_endpoint_unchanged(
            {"provider_name": "p", "provider": "openai", "base_url": "https://old.example"},
            {"provider": "openai", "base_url": "https://new.example"}, role)

    monkeypatch.setattr(DashboardEngine, "_require_endpoint_unchanged", changed)
    resp = client.post("/api/w/w1/remember", json={"text": "x"})
    assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"})


def test_new_world_reads_as_empty_right_after_creation(client):
    """裁定 R34：建世界时 C++ 已建好库文件与 schema。经 HTTP 建完立刻检视：200、空列表，不建引擎。"""
    pool = _pool(client)
    resp = client.post("/api/worlds", json={"name": "fresh"})
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/w/fresh/statements")
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == []
    assert client.get("/api/w/fresh/overview").json()["counts"]["statements"] == 0
    assert "fresh" not in pool.open_worlds()


def test_inspecting_a_world_whose_database_is_missing(client):
    """world_not_initialized 只剩一种情形：登记了、库文件却不在（被手工删掉等）。检视回 409，不替它
    建引擎、不建库。"""
    import os
    pool = _pool(client)
    rec = pool.create_world("gone", "")
    os.remove(rec["db_path"])
    resp = client.get("/api/w/gone/statements")
    assert (resp.status_code, resp.json()) == (409, {"detail": "world_not_initialized"})
    assert "gone" not in pool.open_worlds()
    assert not os.path.exists(rec["db_path"])


def test_ws_converse_targets_the_requested_world(client):
    pool = _pool(client)
    pool.create_world("w1", "")
    with client.websocket_connect("/ws/converse") as ws:
        ws.send_json({"message": "who owns auth?", "world": "w1"})
        while (msg := ws.receive_json())["type"] in ("token", "status"):
            pass
    assert msg["type"] == "done", msg
    assert msg["statement_ids"]
    assert set(_statement_ids(client, "/api/w/w1")) >= set(msg["statement_ids"])
    assert _statement_ids(client, "/api") == []

    with client.websocket_connect("/ws/converse") as ws:
        ws.send_json({"message": "hi", "world": "nope"})
        assert ws.receive_json() == {"type": "error", "error": "world_not_found"}


def test_lifespan_drains_then_closes_every_world(tmp_path, monkeypatch):
    """关停顺序（D-P2-6）：每个开着的世界先进 DRAINING，再停摄入、再停 tick，最后关引擎；池里不再有
    开着的世界，旧 Runtime 都已回收（没有登记成泄漏）。"""
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    events: list[tuple[str, str]] = []
    for name in ("begin_drain", "stop_ingest_worker", "stop_background_tick", "close"):
        original = getattr(DashboardEngine, name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            events.append((self.world, _name))
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(DashboardEngine, name, spy)

    app = create_app(cfg, engine_factory=_stub_factory)
    pool = app.state.pool
    with TestClient(app) as test_client:
        pool.create_world("w1", "")
        _remember(test_client, "/api/w/w1")
        assert pool.open_worlds() == ["default", "w1"]
        threads = []
        for world in ("default", "w1"):
            with pool.lease(world, build=False) as eng:
                threads += [eng._tick_thread, eng._ingest_thread]
            eng = None
        assert all(t is not None and t.is_alive() for t in threads)
        events.clear()
    assert pool.open_worlds() == []
    assert not any(t.is_alive() for t in threads)
    assert pool._leaked == []
    for world in ("default", "w1"):
        steps = [name for w, name in events if w == world]
        # stop_* 在 quiesce 与 close 里各调一次；只看每一步第一次出现的位置。
        first = {name: steps.index(name) for name in dict.fromkeys(steps)}
        assert first["begin_drain"] < first["stop_ingest_worker"] \
            < first["stop_background_tick"] < first["close"], (world, steps)
    # 全部世界都先进 DRAINING，然后才开始停任何一个世界的线程。
    last_drain = max(i for i, (_w, name) in enumerate(events) if name == "begin_drain")
    first_stop = min(i for i, (_w, name) in enumerate(events) if name != "begin_drain")
    assert last_drain < first_stop, events


# ───────────────────────── 丢掉的 app 不留下池与引擎（裁定 R29） ─────────────────────────

def _build_and_discard(tmp_path, monkeypatch, index: int, shape: str):
    """建一个 app、按 shape 用一下、丢掉，只返回弱引用：(池, 默认世界引擎的 Runtime 或 None)。
    FastAPI 在模块级 lru_cache 里留着每个路由函数；路由函数若闭包捕获 app 或池，丢掉的 app 就一直
    活着，连同它开着的 C++ 写连接。"""
    import weakref
    root = tmp_path / f"app{index}"
    cfg = DashboardConfig(db_path=str(root / "dashboard.db"), token="",
                          tick_interval_s=600.0 if shape == "lifespan" else 0,
                          source_lane_enabled=False, ingest_spool_path=str(root / "spool"))
    root.mkdir()
    monkeypatch.setenv("STARLING_DATA_DIR", str(root / "data"))   # 每个 app 各自的注册表
    if shape == "injected":
        app = create_app(cfg, engine=DashboardEngine(cfg))
    else:
        app = create_app(cfg, engine_factory=_stub_factory)
    pool = app.state.pool
    pool_ref = weakref.ref(pool)

    def runtime_ref():
        with pool.lease("default", build=False) as engine:
            return weakref.ref(engine._rt) if engine is not None else None

    if shape == "lifespan":
        with TestClient(app) as test_client:
            _remember(test_client, "/api")
            with test_client.websocket_connect("/ws"):
                pass
            with test_client.websocket_connect("/ws/converse") as ws:
                ws.send_json({"message": ""})
                ws.receive_json()
            rt_ref = runtime_ref()               # 关停之前取：关停会关掉默认世界
            assert rt_ref is not None and rt_ref() is not None
        assert pool.open_worlds() == []
    else:
        test_client = TestClient(app)
        assert test_client.get("/health").status_code == 200
        assert test_client.post("/api/tick", json={}).status_code == 200
        rt_ref = runtime_ref()
        assert rt_ref is not None and rt_ref() is not None
    pool = None
    return pool_ref, rt_ref


@pytest.mark.parametrize("shape", ["plain", "injected", "lifespan"])
def test_discarded_apps_release_pool_and_runtime(tmp_path, monkeypatch, shape):
    import gc
    refs = [_build_and_discard(tmp_path, monkeypatch, index, shape) for index in range(3)]
    gc.collect()
    for pool_ref, rt_ref in refs:
        assert pool_ref() is None, "丢掉的 app 的引擎池还活着"
        assert rt_ref() is None, "丢掉的 app 的默认世界 Runtime 还活着（C++ 写连接没释放）"


# ───────────────────────── 事件循环不等世界锁、引擎锁（裁定 R30、R32） ─────────────────────────

def _health_latency(client, during) -> tuple[float, object]:
    """during() 在另一个线程里跑（它会卡住某个请求）；这期间测一次 GET /health 的耗时。返回
    (健康检查耗时, during 的返回值)。"""
    import threading
    import time
    out = {}

    def run():
        out["result"] = during()
    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.3)                            # 让被卡住的请求进到等锁的那一步
    started = time.monotonic()
    assert client.get("/health").status_code == 200
    latency = time.monotonic() - started
    worker.join(timeout=30)
    assert not worker.is_alive()
    return latency, out.get("result")


def _hold(lock, seconds: float):
    """在另一个线程里持 lock seconds 秒（模拟世界锁内的快照 / 引擎锁内的 tick）。"""
    import threading
    import time
    held = threading.Event()

    def run():
        with lock:
            held.set()
            time.sleep(seconds)
    holder = threading.Thread(target=run)
    holder.start()
    assert held.wait(5)
    return holder


def test_first_open_waits_for_the_world_lock_off_the_event_loop(client):
    """裁定 R32：关着的世界第一次被命令路由打开时要取世界锁建引擎；世界锁被快照占着时，等锁在工作线程
    里，事件循环照常响应别的请求；锁放开后命令照常成功。"""
    pool = _pool(client)
    pool.create_world("w1", "")
    holder = _hold(pool._world_lock("w1"), 2.0)
    latency, resp = _health_latency(client, lambda: client.post("/api/w/w1/tick", json={}))
    holder.join()
    assert latency < 0.3, f"GET /health 等了 {latency:.2f}s：事件循环在等 w1 的世界锁"
    assert resp.status_code == 200, resp.text
    assert "w1" in pool.open_worlds()


def test_ws_converse_first_open_waits_off_the_event_loop(client):
    pool = _pool(client)
    pool.create_world("w1", "")
    holder = _hold(pool._world_lock("w1"), 2.0)

    def converse():
        with client.websocket_connect("/ws/converse") as ws:
            ws.send_json({"message": "who owns auth?", "world": "w1"})
            while (msg := ws.receive_json())["type"] in ("token", "status"):
                pass
            return msg
    latency, msg = _health_latency(client, converse)
    holder.join()
    assert latency < 0.3, f"GET /health 等了 {latency:.2f}s：/ws/converse 在事件循环上等世界锁"
    assert msg["type"] == "done", msg


def test_post_config_waits_for_the_engine_lock_off_the_event_loop(client):
    """裁定 R30：保存配置要换默认世界引擎的设置（取引擎锁）。tick 等长调用持着引擎锁时，等锁在工作线程
    里，事件循环照常响应。"""
    pool = _pool(client)
    with pool.lease("default", build=False) as eng:
        holder = _hold(eng._lock, 2.0)
    eng = None
    latency, resp = _health_latency(
        client, lambda: client.post("/api/config", json={"gist_thresholds": {"min_holders": 4}}))
    holder.join()
    assert latency < 0.3, f"GET /health 等了 {latency:.2f}s：POST /api/config 在事件循环上等引擎锁"
    assert resp.status_code == 200, resp.text


# ───────────────────────── 默认世界在启动时即开（裁定 R33） ─────────────────────────

def test_default_world_opens_at_startup_even_without_tick(tmp_path):
    """tick_interval_s=0 的全新安装：启动后默认世界已开、摄入线程在跑、没有维护 tick，旧路径检视照常 200。"""
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="", tick_interval_s=0,
                          source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"))
    app = create_app(cfg)
    pool = app.state.pool
    with TestClient(app) as test_client:
        assert pool.open_worlds() == ["default"]
        with pool.lease("default", build=False) as eng:
            ingest, tick = eng._ingest_thread, eng._tick_thread
        eng = None
        assert ingest is not None and ingest.is_alive()
        assert tick is None
        assert test_client.get("/api/overview").status_code == 200
        assert test_client.get("/api/runtime_health").status_code == 200
        assert test_client.get("/api/ingest_status").status_code == 200
    assert not ingest.is_alive()


# ───────────────────────── 关停之后的广播桥 ─────────────────────────

def test_bridge_after_the_loop_closed_is_a_quiet_no_op(recwarn):
    """后台线程在起线程时捕获了广播回调；事件循环关掉之后它再调，不抛、不留下没 await 的协程。"""
    import asyncio
    import gc
    from starling.dashboard.realtime import ConnectionManager
    loop = asyncio.new_event_loop()
    callback = ConnectionManager().bridge(loop, "tick")
    loop.close()
    callback("default", {"fired": 0})
    gc.collect()
    assert not [w for w in recwarn if "was never awaited" in str(w.message)]


# ───────────────────────── Task 6b：/api/worlds 的列表、设置、关闭 ─────────────────────────

def test_list_worlds_filters_and_pages(client):
    for index in range(3):
        resp = client.post("/api/worlds", json={"name": f"r1-{index}", "labels": {"run_id": "r1"}})
        assert resp.status_code == 200, resp.text
    for index in range(2):
        client.post("/api/worlds", json={"name": f"r2-{index}", "labels": {"run_id": "r2"}})
    body = client.get("/api/worlds", params={"label.run_id": "r1"}).json()
    assert body["total"] == 3 and body["default"] == "default"
    assert {w["name"] for w in body["worlds"]} == {"r1-0", "r1-1", "r1-2"}
    assert all(w["labels"] == {"run_id": "r1"} for w in body["worlds"])
    page = client.get("/api/worlds", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 6 and len(page["worlds"]) == 2
    rest = client.get("/api/worlds", params={"limit": 2, "offset": 4}).json()
    assert len(rest["worlds"]) == 2
    assert not {w["name"] for w in page["worlds"]} & {w["name"] for w in rest["worlds"]}
    both = client.get("/api/worlds", params={"label.run_id": "r2", "limit": 1}).json()
    assert both["total"] == 2 and len(both["worlds"]) == 1
    # 分页参数由 C++ 校验（limit 1–500、offset >= 0）。
    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        resp = client.get("/api/worlds", params=params)
        assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_world_query"}), params
    # 行里有 open / holders / last_write_at；开着的世界取现读的计数。
    _remember(client, "/api/w/r1-0")
    rows = {w["name"]: w for w in client.get("/api/worlds",
                                             params={"label.run_id": "r1"}).json()["worlds"]}
    assert rows["r1-0"]["open"] is True and rows["r1-0"]["statements"] == 1
    assert rows["r1-0"]["holders"] == 1 and rows["r1-0"]["last_write_at"]
    assert rows["r1-1"]["open"] is False and rows["r1-1"]["statements"] == 0
    assert "active" not in rows["r1-0"]


def test_activate_route_removed(client):
    client.post("/api/worlds", json={"name": "w1"})
    resp = client.post("/api/worlds/w1/activate")
    assert resp.status_code in (404, 405), resp.text


def _secret_client(tmp_path, monkeypatch):
    """默认世界绑了一个带密钥的 provider：新世界继承它的 endpoint（provider_name 指着它）。"""
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: make_stub_llm(default_response=_STUB))
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    cfg.providers["p"] = {"provider": "openai", "model": "m1", "base_url": "http://h.example",
                          "api_key": "sk-secret-123"}
    cfg.roles["extraction"] = "p"
    return TestClient(create_app(cfg, engine_factory=_stub_factory))


def test_settings_update_and_no_secret_leak(tmp_path, monkeypatch):
    with _secret_client(tmp_path, monkeypatch) as client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        before = client.get("/api/worlds/w1").json()["settings"]
        assert before["extraction"]["model"] == "m1" and before["extraction"]["key_set"] is True
        resp = client.put("/api/worlds/w1/settings", json={"extraction": {"model": "m2"}})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["extraction"]["model"] == "m2"
        assert out["extraction"]["key_set"] is True
        assert out["extraction"]["provider_name"] == "p"            # 部分更新：别的叶子不变
        assert "sk-secret" not in resp.text and "api_key" not in resp.text
        # 部分更新不动别的段。
        resp = client.put("/api/worlds/w1/settings", json={"source_lane": {"k_source": 7}})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["source_lane"]["k_source"] == 7 and out["extraction"]["model"] == "m2"
        assert out["background"] == before["background"]
        assert "sk-secret" not in client.get("/api/worlds/w1").text
        assert "sk-secret" not in client.get("/api/worlds").text


def test_settings_put_rejects_key_set_anywhere(client):
    client.post("/api/worlds", json={"name": "w1"})
    before = client.get("/api/worlds/w1").json()["settings"]
    for body in ({"key_set": True}, {"extraction": {"model": "m", "key_set": False}},
                 {"gist_thresholds": {"key_set": 1}}):
        resp = client.put("/api/worlds/w1/settings", json=body)
        assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_settings"}), body
    resp = client.put("/api/worlds/w1/settings", json={"nope": 1})          # C++ 拒未知键
    assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_settings"})
    assert client.get("/api/worlds/w1").json()["settings"] == before
    resp = client.put("/api/worlds/nope/settings", json={})
    assert (resp.status_code, resp.json()) == (404, {"detail": "world_not_found"})


def test_settings_put_on_an_open_world_rebuilds_its_adapters(client, monkeypatch):
    """开着的世界改 endpoint：引擎换上新设置，并按新 endpoint 重建适配器（不留旧适配器）。"""
    import starling.dashboard.engine as engmod
    built = []

    def _record(cfg):
        built.append(dict(cfg))
        return make_stub_llm(default_response=_STUB)
    monkeypatch.setattr(engmod, "_build_chat_adapter", _record)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-service")                  # 显式端点用服务级密钥
    pool = _pool(client)
    client.post("/api/worlds", json={"name": "w1"})
    _remember(client, "/api/w/w1")
    assert "w1" in pool.open_worlds()
    with pool.lease("w1", build=False) as eng:
        old_llm = eng.llm
    eng = None
    resp = client.put("/api/worlds/w1/settings", json={"extraction": {
        "provider": "openai", "base_url": "http://new.example", "model": "m-new"}})
    assert resp.status_code == 200, resp.text
    with pool.lease("w1", build=False) as eng:
        assert eng._settings["extraction"]["model"] == "m-new"
        assert eng.llm is not old_llm
        new_llm = eng.llm
    eng = None
    extraction = [cfg for cfg in built if cfg.get("model") == "m-new"]
    assert extraction and extraction[0]["base_url"] == "http://new.example"
    assert extraction[0]["api_key"] == "sk-service"
    # 只改别的段：抽取适配器不重建。
    client.put("/api/worlds/w1/settings", json={"gist_thresholds": {"min_holders": 4}})
    with pool.lease("w1", build=False) as eng:
        assert eng.llm is new_llm
        assert eng._settings["gist_thresholds"] == {"min_holders": 4.0}
    eng = None
    del old_llm, new_llm


def test_settings_put_on_the_default_world_takes_only_background(client):
    """默认世界的 endpoint、阈值等段归 /api/config（下次同步会覆盖），这里只收 background。"""
    resp = client.put("/api/worlds/default/settings",
                      json={"background": {"auto_snapshot": {"on_session": True,
                                                             "interval_hours": 6}}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["background"]["auto_snapshot"] == {"on_session": True, "interval_hours": 6}
    before = client.get("/api/worlds/default").json()["settings"]
    for body in ({"extraction": {"model": "m2"}}, {"gist_thresholds": {"min_holders": 2}},
                 {"background": {"tick_interval_s": 5.0}, "source_lane": {"k_source": 3}}):
        resp = client.put("/api/worlds/default/settings", json=body)
        assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_settings"}), body
    assert client.get("/api/worlds/default").json()["settings"] == before


def test_close_world_route(client):
    pool = _pool(client)
    client.post("/api/worlds", json={"name": "w1"})
    written = _remember(client, "/api/w/w1")
    assert "w1" in pool.open_worlds()
    resp = client.post("/api/worlds/w1/close")
    assert (resp.status_code, resp.json()) == (200, {"closed": "w1"})
    assert "w1" not in pool.open_worlds()
    assert pool._leaked == []
    # 关闭时写了统计缓存：列表里关着的世界照样有计数。
    row = next(w for w in client.get("/api/worlds").json()["worlds"] if w["name"] == "w1")
    assert (row["open"], row["statements"]) == (False, 1)
    # 关着的世界照样能检视；下一次命令重新打开它。
    assert _statement_ids(client, "/api/w/w1") == written["statement_ids"]
    assert client.post("/api/worlds/w1/close").json() == {"closed": "w1"}   # 没开着：照样 200
    resp = client.post("/api/worlds/default/close")
    assert (resp.status_code, resp.json()) == (409, {"detail": "world_protected"})
    resp = client.post("/api/worlds/nope/close")
    assert (resp.status_code, resp.json()) == (404, {"detail": "world_not_found"})
    _remember(client, "/api/w/w1", "Carol owns billing")
    assert "w1" in pool.open_worlds()


def test_world_detail_reports_sizes_and_effective_settings(client):
    client.post("/api/worlds", json={"name": "w1", "label": "一号", "labels": {"k": "v"}})
    _remember(client, "/api/w/w1")
    detail = client.get("/api/worlds/w1").json()
    assert detail["name"] == "w1" and detail["label"] == "一号" and detail["labels"] == {"k": "v"}
    assert detail["db_bytes"] > 0 and detail["live_bytes"] > 0 and detail["engrams"] >= 1
    assert detail["statements"] == 1 and detail["open"] is True
    assert detail["settings"]["vector_backend"] == "sqlite"
    resp = client.get("/api/worlds/nope")
    assert (resp.status_code, resp.json()) == (404, {"detail": "world_not_found"})


def test_create_world_with_labels_settings_and_legacy_auto_snapshot(client):
    resp = client.post("/api/worlds", json={
        "name": "w1", "labels": {"run_id": "r9"},
        "settings": {"source_lane": {"k_source": 11}},
        "auto_snapshot": {"on_session": False, "interval_hours": 12}})
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["labels"] == {"run_id": "r9"} and row["open"] is False
    assert row["auto_snapshot"] == {"on_session": False, "interval_hours": 12}
    settings = client.get("/api/worlds/w1").json()["settings"]
    assert settings["source_lane"]["k_source"] == 11
    assert settings["background"]["auto_snapshot"] == {"on_session": False, "interval_hours": 12}
    for body in ({"name": "w2", "settings": {"key_set": True}},
                 {"name": "w2", "settings": {"bogus": 1}}):
        resp = client.post("/api/worlds", json=body)
        assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_settings"}), body
    assert client.get("/api/worlds/w2").status_code == 404


def test_first_build_of_a_world_with_foreign_tenant_rows_is_refused(client):
    """裁定 R36：非默认世界第一次建引擎之前，C++ 核对库里只有它自己的租户；有别的租户的行就拒绝
    （409 world_tenant_mismatch），什么都不建、不发布。"""
    import sqlite3
    pool = _pool(client)
    rec = pool.create_world("w1", "")
    con = sqlite3.connect(rec["db_path"])
    con.execute(
        "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
        "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
        "VALUES('x-1','intruder','hx','conversation','default','inline','normal',"
        "'audit_retain','2026-10-25T00:00:00Z','x-1')")
    con.commit()
    con.close()
    resp = client.post("/api/w/w1/remember", json={"text": "x"})
    assert (resp.status_code, resp.json()) == (409, {"detail": "world_tenant_mismatch"})
    assert "w1" not in pool.open_worlds() and pool._leaked == []
    # 检视（只读、不建引擎）照常；默认世界不核对（与旧的启动时一致）。
    assert client.get("/api/w/w1/statements").status_code == 200
    assert client.post("/api/tick", json={}).status_code == 200


# ───────────────────────── 裁定 R38：客户端写设置之前核对继承来的地址 ─────────────────────────

def _embedding_client(tmp_path, monkeypatch, built: list):
    """默认世界的 extraction 与 embedding 都绑 provider p（带密钥）；新世界继承它们，provider_name 指着 p。
    嵌入适配器换成记录配置的占位，不发网络请求。"""
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: make_stub_llm(default_response=_STUB))

    def _embed(cfg):
        built.append(dict(cfg))
        return _core.StubEmbeddingAdapter(8)
    monkeypatch.setattr(engmod, "_build_embed_adapter", _embed)
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    cfg.providers["p"] = {"provider": "openai", "model": "m1", "base_url": "http://h.example",
                          "api_key": "sk-secret-123"}
    cfg.roles["extraction"] = "p"
    cfg.roles["embedding"] = "p"
    return TestClient(create_app(cfg, engine_factory=_stub_factory))


def _vector_rows(db_path: str) -> int:
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM statement_vectors").fetchone()[0]
    finally:
        con.close()


def test_put_settings_cannot_redirect_an_inherited_endpoint(tmp_path, monkeypatch):
    """审查探针：继承来的 embedding（provider_name=p）只改 base_url，C++ 叶合并留着 provider_name。若照写，
    重建嵌入适配器会带着 p 的密钥指向新地址并立刻重嵌入（先删光向量）。写之前核对：409 endpoint_changed，
    注册表、引擎、向量都不动，也没有用存着的密钥建过适配器。"""
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        pool = _pool(client)
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        assert client.post("/api/w/w1/tick", json={}).status_code == 200      # 嵌入向量
        db = pool.registry.get("w1")["db_path"]
        vectors = _vector_rows(db)
        assert vectors >= 1
        before = pool.registry.get("w1")["settings"]
        built.clear()
        for body in ({"embedding": {"base_url": "http://evil.example"}},
                     {"extraction": {"base_url": "http://evil.example"}},
                     {"chat": {"provider_name": "p", "base_url": "http://evil.example",
                               "provider": "openai", "model": "m"}}):
            resp = client.put("/api/worlds/w1/settings", json=body)
            assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), body
        assert pool.registry.get("w1")["settings"] == before
        assert built == [], "用存着的密钥建了指向新地址的适配器"
        assert _vector_rows(db) == vectors
        with pool.lease("w1", build=False) as eng:
            assert eng._settings["embedding"]["base_url"] == "http://h.example"
        eng = None
        assert client.post("/api/w/w1/tick", json={}).status_code == 200      # 世界照常可用
        # 解绑（null）与改别的叶子（同一地址）照常放行。
        resp = client.put("/api/worlds/w1/settings", json={"embedding": {"model": "e2"}})
        assert resp.status_code == 200, resp.text
        assert built and built[-1]["base_url"] == "http://h.example"


def test_fork_with_settings_cannot_point_an_inherited_endpoint_elsewhere(tmp_path, monkeypatch):
    """裁定 R31 M3：分叉带设置覆盖时，与建世界一样把 provider 地址表交给 C++，在登记的那次持锁内核对继承来的
    endpoint。改了地址的覆盖回 409 endpoint_changed，不留目录、不登记；不碰地址的覆盖照常分叉。"""
    import os
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        pool = _pool(client)
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        worlds_root = os.path.join(pool.registry.data_dir, "worlds")
        dirs = set(os.listdir(worlds_root))
        for body in ({"new_name": "f1", "settings": {"embedding": {"base_url": "http://evil.example"}}},
                     {"new_name": "f1", "settings": {"extraction": {"base_url": "http://evil.example"}}}):
            resp = client.post("/api/worlds/w1/fork", json=body)
            assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), body
        assert client.get("/api/worlds/f1").status_code == 404
        assert set(os.listdir(worlds_root)) == dirs
        pool.close_world("w1")                                       # 关着的来源走 world_fork，同样核对
        resp = client.post("/api/worlds/w1/fork", json={
            "new_name": "f1", "settings": {"embedding": {"base_url": "http://evil.example"}}})
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"})
        assert set(os.listdir(worlds_root)) == dirs
        resp = client.post("/api/worlds/w1/fork", json={"new_name": "f1",
                                                        "settings": {"source_lane": {"k_source": 9}}})
        assert resp.status_code == 200, resp.text
        assert pool.registry.get("f1")["settings"]["embedding"]["base_url"] == "http://h.example"


def test_create_world_cannot_point_an_inherited_endpoint_elsewhere(tmp_path, monkeypatch):
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        pool = _pool(client)
        for body in ({"name": "w1", "settings": {"embedding": {"base_url": "http://evil.example"}}},
                     {"name": "w1", "settings": {"extraction": {"base_url": "http://evil.example"}},
                      "auto_snapshot": {"on_session": True, "interval_hours": 0}}):
            resp = client.post("/api/worlds", json=body)
            assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), body
        assert client.get("/api/worlds/w1").status_code == 404
        assert [w["name"] for w in client.get("/api/worlds").json()["worlds"]] == ["default"]
        resp = client.post("/api/worlds", json={"name": "w1",
                                                "auto_snapshot": {"on_session": True,
                                                                  "interval_hours": 0}})
        assert resp.status_code == 200, resp.text                # 只带旧字段：照常建
        assert pool.registry.get("w1")["settings"]["embedding"]["base_url"] == "http://h.example"


def test_put_settings_with_an_explicit_endpoint_uses_only_the_service_key(tmp_path, monkeypatch):
    """provider_name 为空的显式端点指向自定义地址照常放行：适配器只带服务级密钥（裁定 R22），不带 p 的密钥。"""
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-service")
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/tick", json={}).status_code == 200
        built.clear()
        resp = client.put("/api/worlds/w1/settings", json={"embedding": {
            "provider_name": "", "provider": "openai", "base_url": "http://mine.example",
            "model": "e-mine", "dim": 8}})
        assert resp.status_code == 200, resp.text
        assert built and built[-1]["base_url"] == "http://mine.example"
        assert built[-1]["api_key"] == "sk-service"
        assert all(cfg.get("api_key") != "sk-secret-123" for cfg in built)
        assert "sk-" not in resp.text


def test_list_skips_a_world_deleted_between_listing_and_building_its_row(client, monkeypatch):
    """注册表列出之后、建某一行之前那个世界被删掉：跳过这一行，列表照常 200，不是整张 404。"""
    pool = _pool(client)
    for name in ("w1", "w2"):
        assert client.post("/api/worlds", json={"name": name}).status_code == 200
    real = pool.snapshots
    deleted: list = []

    def _delete_w1_first(world):
        if world == "w1" and not deleted:
            deleted.append(world)
            pool.delete_world("w1")                 # 恰好落在列出与建行之间
        return real(world)
    monkeypatch.setattr(pool, "snapshots", _delete_w1_first)
    resp = client.get("/api/worlds")
    assert resp.status_code == 200, resp.text
    names = [w["name"] for w in resp.json()["worlds"]]
    assert deleted == ["w1"] and "w1" not in names and {"w2", "default"} <= set(names)


# ───────────────────────── 裁定 R39：路由的租约覆盖锁外阶段，关闭与恢复等它 ─────────────────────────

def _until(predicate, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _runtime_refs(pool, monkeypatch) -> list:
    """记下池此后建的每个引擎的 Runtime（弱引用）。"""
    import weakref
    refs: list = []
    real = pool._open_engine

    def _track(world, ctx):
        engine = real(world, ctx)
        refs.append(weakref.ref(engine._rt))
        return engine
    monkeypatch.setattr(pool, "_open_engine", _track)
    return refs


def _paused_extraction(monkeypatch):
    """remember 的锁外抽取段（remember_extract）卡在 Event 上：返回 (进入了, 放行)。"""
    from starling.dashboard import engine as engmod
    entered, release = threading.Event(), threading.Event()
    real = engmod.MemoryCore.remember_extract

    def _paused(self, bundle, **kwargs):
        entered.set()
        assert release.wait(15), "测试没有放行抽取"
        return real(self, bundle, **kwargs)
    monkeypatch.setattr(engmod.MemoryCore, "remember_extract", _paused)
    return entered, release


@pytest.mark.parametrize("operation", ["close", "rollback"])
def test_close_and_rollback_wait_for_a_remember_paused_in_extraction(client, monkeypatch, operation):
    """remember 的抽取段在引擎锁外。命令路由的租约要覆盖整条请求：关闭或恢复这个世界在抽取被卡住时一直等，
    放行之后请求先在原引擎上提交、关闭或恢复后完成；之后只剩一个 Runtime 活着（同一个库上没有第二条写连接）。"""
    import gc
    pool = _pool(client)
    pool.create_world("w1", "")
    refs = _runtime_refs(pool, monkeypatch)
    _remember(client, "/api/w/w1", "Alice owns ops")
    snap = pool.take_snapshot("w1", "") if operation == "rollback" else None
    entered, release = _paused_extraction(monkeypatch)
    responses: list = []
    req = threading.Thread(target=lambda: responses.append(
        client.post("/api/w/w1/remember", json={"text": "Bob owns auth"})), daemon=True)
    req.start()
    assert entered.wait(10), "remember 没走到锁外抽取段"
    done = threading.Event()
    run = (lambda: pool.close_world("w1")) if operation == "close" \
        else (lambda: pool.rollback("w1", snap["id"]))
    worker = threading.Thread(target=lambda: (run(), done.set()), daemon=True)
    worker.start()
    try:
        assert _until(lambda: "w1" in pool.closing_worlds()), f"{operation} 没开始"
        assert not done.wait(0.3), f"remember 还在飞，{operation} 不该完成"
        assert "w1" in pool.open_worlds(), "请求还持着租约，引擎却已被丢"
    finally:
        release.set()
    req.join(15)
    assert done.wait(15), f"{operation} 在请求结束之后没完成"
    worker.join(5)
    assert responses and responses[0].status_code == 200, responses
    gc.collect()
    alive = [ref for ref in refs if ref() is not None]
    if operation == "close":
        assert alive == [] and "w1" not in pool.open_worlds()
        assert pool.registry.get("w1")["statements"] == 2        # 请求在关闭之前提交了
    else:
        assert len(alive) == 1 and "w1" in pool.open_worlds()    # 恢复后重开的那一个
        with pool.lease("w1", build=False) as eng:
            assert alive[0]() is eng._rt
        eng = None
    assert pool._leaked == []


def test_close_waits_for_an_in_flight_converse_turn(client, monkeypatch):
    """/ws/converse 的生成段在引擎锁外；run_blocking 持着本轮的租约直到用完引擎。生成被卡住时关闭在等，放行后
    这一轮先 done，关闭随后完成，旧 Runtime 随之释放。"""
    import gc
    pool = _pool(client)
    pool.create_world("w1", "")
    refs = _runtime_refs(pool, monkeypatch)
    generating, release = threading.Event(), threading.Event()

    def _blocked_stream(self, message, **kwargs):
        generating.set()
        assert release.wait(15), "测试没有放行这一轮"
        return {"reply": "ok", "statement_ids": [], "remember_ok": True}
    monkeypatch.setattr(DashboardEngine, "converse_stream", _blocked_stream)
    frames: list = []

    def _turn():
        with client.websocket_connect("/ws/converse") as ws:
            ws.send_json({"message": "who owns auth?", "world": "w1"})
            while (msg := ws.receive_json())["type"] not in ("done", "error"):
                frames.append(msg)
            frames.append(msg)
    turn = threading.Thread(target=_turn, daemon=True)
    turn.start()
    assert generating.wait(10), "这一轮没走到生成段"
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (pool.close_world("w1"), closed.set()), daemon=True)
    closer.start()
    try:
        assert _until(lambda: "w1" in pool.closing_worlds()), "关闭没开始"
        assert not closed.wait(0.3), "这一轮还在生成，关闭不该完成"
        assert "w1" in pool.open_worlds()
    finally:
        release.set()
    turn.join(15)
    assert closed.wait(15)
    closer.join(5)
    assert frames and frames[-1]["type"] == "done", frames
    monkeypatch.undo()
    gc.collect()
    assert refs and all(ref() is None for ref in refs), "这一轮结束、世界关了，旧 Runtime 必须已释放"


def test_rollback_of_the_default_world_waits_for_a_config_hot_swap(client, monkeypatch):
    """POST /api/config 在工作线程里持默认世界的一份租约做完整段热换（_write_config）。热换被卡住时恢复默认世界
    一直等；放行之后配置请求先 200，恢复随后完成。"""
    from starling.dashboard import engine as engmod
    pool = _pool(client)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda cfg: make_stub_llm(default_response=_STUB))
    snap = pool.take_snapshot("default", "")
    applying, release = threading.Event(), threading.Event()
    real_reload = DashboardEngine.reload_adapters

    def _blocked_reload(self, roles, **kwargs):
        applying.set()
        assert release.wait(15), "测试没有放行热换"
        return real_reload(self, roles, **kwargs)
    monkeypatch.setattr(DashboardEngine, "reload_adapters", _blocked_reload)
    responses: list = []
    body = {"providers": {"p": {"provider": "openai", "model": "m", "api_key": "sk-x"}},
            "roles": {"extraction": "p"}}
    req = threading.Thread(target=lambda: responses.append(client.post("/api/config", json=body)),
                           daemon=True)
    req.start()
    assert applying.wait(10), "热换没走到重建适配器"
    done = threading.Event()
    results: list = []
    roller = threading.Thread(target=lambda: (results.append(pool.rollback("default", snap["id"])),
                                              done.set()), daemon=True)
    roller.start()
    try:
        assert _until(lambda: "default" in pool.closing_worlds()), "恢复没开始"
        assert not done.wait(0.3), "热换还持着默认世界的租约，恢复不该完成"
    finally:
        release.set()
    req.join(15)
    assert done.wait(15)
    roller.join(5)
    assert responses and responses[0].status_code == 200, responses[0].text
    assert results and results[0]["reopened"] is True


def test_ws_converse_against_a_closing_world_gets_a_world_busy_frame(client):
    pool = _pool(client)
    pool.create_world("w1", "")
    with pool._lock:
        pool._closing.add("w1")
    try:
        with client.websocket_connect("/ws/converse") as ws:
            ws.send_json({"message": "who owns auth?", "world": "w1"})
            assert ws.receive_json() == {"type": "error", "error": "world_busy"}
    finally:
        with pool._lock:
            pool._closing.discard("w1")
    assert "w1" not in pool.open_worlds()                   # 被拒的这一轮没建引擎
    with client.websocket_connect("/ws/converse") as ws:      # 关闭窗口一过照常对话
        ws.send_json({"message": "who owns auth?", "world": "w1"})
        while (msg := ws.receive_json())["type"] in ("token", "status"):
            pass
        assert msg["type"] == "done", msg


# ───────────────────────── 裁定 R40、R41：核对与写原子；引擎按建适配器时的 endpoint 核对 ─────────────────────────

def _window_client(tmp_path, monkeypatch, built: list):
    """默认世界只绑 extraction 到 p（embedding 没绑）。嵌入适配器换成记录 (base_url, api_key) 的占位。"""
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: make_stub_llm(default_response=_STUB))

    def _embed(cfg):
        built.append((cfg.get("base_url"), cfg.get("api_key")))
        return _core.StubEmbeddingAdapter(8)
    monkeypatch.setattr(engmod, "_build_embed_adapter", _embed)
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    cfg.providers["p"] = {"provider": "openai", "model": "m1", "base_url": "http://h.example",
                          "api_key": "sk-secret-123"}
    cfg.roles["extraction"] = "p"
    return cfg, TestClient(create_app(cfg, engine_factory=_stub_factory))


def test_create_racing_a_config_bind_never_stores_a_provider_name_at_the_client_url(tmp_path,
                                                                                   monkeypatch):
    """审查探针 test_probe_create_window：建世界时客户端给显式的 embedding（默认世界那一刻没绑 embedding），
    合并之前 POST /api/config 把 embedding 绑到 p。C++ 叶合并会把客户端的 base_url 叠到 p 的 endpoint 上。
    核对与合并、写在同一次持锁内，所以 409、世界不存在。用真实的同步插在 C++ 读默认世界设置之前（包一层
    reg.create，先同步再调真的）。"""
    built: list = []
    cfg, client = _window_client(tmp_path, monkeypatch, built)
    with client:
        pool = _pool(client)
        reg = pool.registry

        class _RacingRegistry:
            """把 create 包一层：调 C++ 之前先做一次 config 绑定与同步（模拟并发的 POST /api/config）。"""
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def create(self, *args, **kwargs):
                cfg.roles["embedding"] = "p"
                pool.sync_default_settings()
                return self._inner.create(*args, **kwargs)
        pool._registry = _RacingRegistry(reg)
        try:
            resp = client.post("/api/worlds", json={"name": "w1", "settings": {
                "embedding": {"base_url": "http://evil.example", "model": "e", "dim": 8}}})
        finally:
            pool._registry = reg
        # 插入点是确定的（同步一定落在 C++ 合并之前），所以结果也是确定的：叶合并叠出 provider_name=p、
        # base_url=客户端地址，C++ 在同一次持锁内核对出来，拒绝。
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), resp.text
        assert client.get("/api/worlds/w1").status_code == 404
        assert all(b != ("http://evil.example", "sk-secret-123") for b in built)


def test_settings_swapped_without_rebuild_never_leave_an_adapter_built_elsewhere(tmp_path,
                                                                                 monkeypatch):
    """审查探针 test_probe_chain（R41）：引擎的嵌入适配器是按某个 endpoint 建的；设置被换成另一个 endpoint 却没
    重建适配器（例如 set_auto_snapshot 落在 update_settings 与 apply_world_settings 之间，apply 看不出变化）。
    下一次用到嵌入时，核对的是适配器建时的 endpoint，不是 _settings 里的；两者不同就先按 _settings 核对并重建。
    这里直接造出这种状态：引擎的嵌入适配器建在客户端地址、带着 p 的密钥（R40 之前的建世界窗口能造出来），
    然后把 _settings 换成 p 的真实地址。tick 200，嵌入适配器已按 p 的真实地址重建；
    之后不再用建在客户端地址的那个。"""
    built: list = []
    cfg, client = _window_client(tmp_path, monkeypatch, built)
    with client:
        pool = _pool(client)
        cfg.roles["embedding"] = "p"
        pool.sync_default_settings()
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        evil = {"provider": "openai", "provider_name": "p", "base_url": "http://evil.example",
                "model": "e", "dim": 8}
        with pool.lease("w1") as eng:
            good_settings = eng._settings
            eng._settings = {**good_settings, "embedding": {**good_settings["embedding"], **evil}}
            eng.rebuild_embedder(eng._endpoint_cfg("embedding"), reembed=False)   # 建在 evil
            assert built[-1] == ("http://evil.example", "sk-secret-123")
            eng._settings = good_settings                      # 设置换回 p 的地址，适配器没重建
        eng = None
        built.clear()
        uses: list = []
        real_tick = _core_tick_spy(monkeypatch, pool, uses)
        resp = client.post("/api/w/w1/tick", json={})
        # 裁定 R54：tick 不因嵌入端点被拒而 409，只跳过嵌入阶段。这里 _settings 的地址是 p 的真实地址，核对通过，
        # 所以嵌入适配器按它重建、嵌入阶段照常跑，不再用建在客户端地址的那个。
        assert resp.status_code == 200, resp.text
        assert "embed" not in resp.json()["stages_skipped"]
        assert built and built[0] == ("http://h.example", "sk-secret-123"), built
        assert uses and all(url == "http://h.example" for url in uses), uses
        del real_tick


def _core_tick_spy(monkeypatch, pool, uses: list):
    """记下每次 MemoryCore.tick 时引擎嵌入适配器建时的地址（DashboardEngine._built_endpoints）。"""
    from starling.dashboard import engine as engmod
    real = engmod.MemoryCore.tick

    def _spy(self, now, **kwargs):
        with pool.lease("w1", build=False) as eng:
            built = getattr(eng, "_built_endpoints", {}).get("embedding") or {}
            if not kwargs.get("skip_embed"):          # 跳过嵌入阶段的一轮不用嵌入适配器（裁定 R54）
                uses.append(built.get("base_url"))
        eng = None
        return real(self, now, **kwargs)
    monkeypatch.setattr(engmod.MemoryCore, "tick", _spy)
    return real


# ───────────────────────── 裁定 R43：选了 provider 的对话照样核对世界自己的抽取与嵌入 ─────────────────────────

def _converse_client(tmp_path, monkeypatch, origin: dict, used: list):
    """默认世界的 extraction 绑 p；另有 provider q。每个建出来的对话/抽取适配器记下 (base_url, api_key)；
    converse_commit 记下它用的世界抽取适配器是按哪个地址建的（审查探针 test_probe_converse_provider）。"""
    import starling._memory_core as mc
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def _chat(c):
        llm = make_stub_llm(default_response="[]")
        origin[id(llm)] = (c.get("base_url"), c.get("api_key"))
        return llm
    monkeypatch.setattr(engmod, "_build_chat_adapter", _chat)
    real_commit = mc.MemoryCore.converse_commit

    def _spy(self, *a, **kw):
        used.append(origin.get(id(self.llm)))
        return real_commit(self, *a, **kw)
    monkeypatch.setattr(mc.MemoryCore, "converse_commit", _spy)
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    cfg.providers["p"] = {"provider": "openai", "model": "m1", "base_url": "http://h.example",
                          "api_key": "sk-secret-123"}
    cfg.providers["q"] = {"provider": "openai", "model": "m1", "base_url": "http://q.example",
                          "api_key": "sk-q"}
    cfg.roles["extraction"] = "p"
    return cfg, TestClient(create_app(cfg, engine_factory=lambda ctx, prov: DashboardEngine(ctx, prov)))


def _ws_converse(client, body: dict) -> dict:
    with client.websocket_connect("/ws/converse") as ws:
        ws.send_json(body)
        while (msg := ws.receive_json())["type"] in ("token", "status"):
            pass
    return msg


def test_converse_with_a_provider_still_checks_the_worlds_own_endpoints(tmp_path, monkeypatch):
    """审查探针：w2 继承 p@h.example，随后 p 挪到 h2。不选 provider 的对话 409；选了 q 的对话以前直接 200，回合末的
    抽取用的是建在 h.example、带着 p 的密钥的世界适配器。现在选了 provider 也先核对世界的抽取与嵌入：HTTP 409、
    /ws/converse 回 endpoint_changed 错误帧，没有用到建在 p 旧地址的适配器。"""
    origin: dict = {}
    used: list = []
    cfg, client = _converse_client(tmp_path, monkeypatch, origin, used)
    with client:
        assert client.post("/api/worlds", json={"name": "w2"}).status_code == 200
        cfg.providers["p"]["base_url"] = "http://h2.example"      # p 挪了；w2 记的还是 h.example
        for body in ({"message": "hi"}, {"message": "hi", "provider": "q"}):
            r = client.post("/api/w/w2/converse", json=body)
            assert (r.status_code, r.json()) == (409, {"detail": "endpoint_changed"}), body
            frame = _ws_converse(client, {**body, "world": "w2"})
            assert frame == {"type": "error", "error": "endpoint_changed"}, (body, frame)
        assert used == [], "回合末的抽取用到了建在 p 旧地址的适配器"


def test_converse_with_a_provider_works_when_nothing_moved(tmp_path, monkeypatch):
    origin: dict = {}
    used: list = []
    _cfg, client = _converse_client(tmp_path, monkeypatch, origin, used)
    with client:
        assert client.post("/api/worlds", json={"name": "w2"}).status_code == 200
        r = client.post("/api/w/w2/converse", json={"message": "hi", "provider": "q"})
        assert r.status_code == 200, r.text
        assert _ws_converse(client, {"message": "hi", "provider": "q", "world": "w2"})["type"] == "done"
        assert used == [("http://h.example", "sk-secret-123")] * 2   # 世界自己的抽取，地址没变


def test_provider_endpoints_snapshot_survives_a_concurrent_config_edit(client, monkeypatch):
    """_provider_endpoints 遍历 cfg.providers 时，/api/config 的另一个线程在改它（setdefault、clear + update）。先拍
    快照再遍历：不抛「dictionary changed size during iteration」。卡点：读第一个 provider 的 base_url 时（遍历的
    途中）往同一个 dict 里加一项，就是并发的 setdefault 落在两次迭代之间。"""
    pool = _pool(client)
    providers = client.app.state.config.providers
    providers.clear()
    inserted: list = []

    class _Prov(dict):
        def get(self, key, default=None):
            if key == "base_url" and not inserted:
                inserted.append(True)
                providers.setdefault("late", {"base_url": "http://late"})
            return super().get(key, default)
    providers["p"] = _Prov(base_url="http://h")
    providers["q"] = {"base_url": "http://q"}
    urls = pool._provider_endpoints()
    assert inserted and urls["p"] == {"provider": "openai", "base_url": "http://h"}
    assert urls["q"] == {"provider": "openai", "base_url": "http://q"}


# ───────────────────────── 裁定 R51：端点身份 = (provider 种类, base_url) ─────────────────────────

def _recording_client(tmp_path, monkeypatch, built: list, provider: dict):
    """默认世界的 extraction 绑 p（provider 由调用方给）。每个建出来的抽取/对话适配器记下
    (provider 种类, base_url, api_key)；引擎照常从设置建适配器（不装替身），所以记下的就是真实建法。"""
    import starling.dashboard.engine as engmod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def _chat(cfg):
        built.append(((cfg.get("provider") or "openai"), cfg.get("base_url", ""), cfg.get("api_key")))
        return make_stub_llm(default_response=_STUB)
    monkeypatch.setattr(engmod, "_build_chat_adapter", _chat)
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    cfg.providers["p"] = dict(provider)
    cfg.roles["extraction"] = "p"
    return cfg, TestClient(create_app(cfg, engine_factory=lambda ctx, prov: DashboardEngine(ctx, prov)))


@pytest.mark.parametrize("before, after", [("openai", "anthropic"), ("anthropic", "openai")])
def test_kind_change_with_an_empty_base_url_refuses_the_inherited_endpoint(tmp_path, monkeypatch,
                                                                         before, after):
    """审查 C-1：p 的 base_url 为空（种类默认地址），w1 继承 p。设置页把 p 改成另一种类、换新密钥，base_url 仍为空。
    以前只比 base_url（"" == ""），w1 会按旧种类建适配器、带着新密钥发到旧种类的默认地址。现在种类也比：409
    endpoint_changed，w1 没有用新密钥建过任何适配器。"""
    built: list = []
    _cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                     {"provider": before, "model": "m1", "base_url": "",
                                      "api_key": "sk-old"})
    with client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        resp = client.post("/api/config", json={"providers": {
            "p": {"provider": after, "api_key": "sk-new"}}})
        assert resp.status_code == 200, resp.text
        assert client.app.state.config.providers["p"]["base_url"] == ""
        built.clear()                                   # 默认世界的热换已在上面完成
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), resp.text
        assert all(key != "sk-new" for _kind, _url, key in built), built
        # 默认世界跟着 config 走：按新种类、新密钥照常写。
        assert client.post("/api/remember", json={"text": "Carol owns billing"}).status_code == 200


# ───────────────────────── 裁定 R52：开着的非默认世界用 provider 当前的密钥 ─────────────────────────

def test_key_only_config_edit_reaches_an_open_non_default_world(tmp_path, monkeypatch):
    """审查 I-1：默认世界与 w1 都开着，w1 继承 p。POST /api/config 只换 p 的密钥（地址、种类不变）。以前只有默认世界
    重建适配器，w1 一直带着旧密钥。现在 w1 下一次 remember 按新密钥建适配器。"""
    built: list = []
    _cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                     {"provider": "openai", "model": "m1",
                                      "base_url": "http://h.example", "api_key": "sk-old"})
    with client:
        pool = _pool(client)
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        assert sorted(pool.open_worlds()) == ["default", "w1"]
        resp = client.post("/api/config", json={"providers": {"p": {"api_key": "sk-new"}}})
        assert resp.status_code == 200, resp.text
        assert "sk-" not in resp.text
        built.clear()
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"}).status_code == 200
        assert ("openai", "http://h.example", "sk-new") in built, built
        assert all(key != "sk-old" for _kind, _url, key in built), built
        built.clear()                                   # 指纹已对上：不再重建
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth 3"}).status_code == 200
        assert built == []


def test_removed_key_makes_an_open_world_refuse_like_an_unconfigured_role(tmp_path, monkeypatch):
    """p 的密钥被删掉（地址不变）：w1 下一次 remember 按「没有密钥」重建，抽取为 None，回 409 llm_not_configured，
    不再用旧密钥。"""
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-old"})
    with client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        built.clear()
        cfg.providers["p"].pop("api_key")              # POST /api/config 的空密钥是「保留」，这里直接删
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "llm_not_configured"}), resp.text
        assert built == []
        with _pool(client).lease("w1", build=False) as eng:
            assert eng.llm is None
        eng = None


def _mutate_inside_the_check(monkeypatch, provider: dict):
    """把引擎核对用的 provider_endpoint 包一层：第一次调用先按传进来的条目算出（旧的）身份，再原地改共享的 provider
    条目（种类 → anthropic、密钥 → sk-new，同 POST /api/config 的 _merge_provider 先写种类后写密钥），最后返回旧身份。
    就是审查 N-1 的交错：核对读到旧种类、放行，保存随后改了种类与密钥，之后再读密钥就读到新的。"""
    import starling.dashboard.engine as engmod
    real = engmod.provider_endpoint
    fired: list = []

    def _racing(prov):
        out = real(prov)
        if not fired:
            fired.append(True)
            provider["provider"] = "anthropic"
            provider["api_key"] = "sk-new"
        return out
    monkeypatch.setattr(engmod, "provider_endpoint", _racing)
    return fired


def test_config_save_racing_a_key_rotation_rebuild_never_pairs_the_old_kind_with_the_new_key(
        tmp_path, monkeypatch):
    """审查 N-1（裁定 R57）：w1 继承 p，适配器按 sk-old 建。先只换 p 的密钥（sk-mid），w1 的下一次用到要按新密钥重建
    （R52）。这次重建的核对与读密钥之间，POST /api/config 把 p 改成 anthropic、密钥 sk-new。以前核对读旧种类放行、重建
    读到 sk-new，按世界记的 openai 建出 (openai, sk-new)：Anthropic 的密钥发到 OpenAI 兼容的地址。现在一次重建只读一份
    provider 快照：要么整份是改之前的（按 openai + sk-mid 建），要么整份是改之后的（endpoint_changed），从不混搭。快照在
    保存之前拍的，所以这一次照常完成；下一次用到 p 时核对出种类变了，endpoint_changed。"""
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-old"})
    with client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        resp = client.post("/api/config", json={"providers": {"p": {"api_key": "sk-mid"}}})
        assert resp.status_code == 200, resp.text
        built.clear()                                   # 默认世界的热换已在上面完成
        fired = _mutate_inside_the_check(monkeypatch, cfg.providers["p"])
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"})
        assert fired, "the provider entry was never mutated mid-check"
        assert resp.status_code in (200, 409), resp.text
        assert ("openai", "http://h.example", "sk-new") not in built, built
        assert all(key != "sk-new" for _kind, _url, key in built), built
        assert all(build == ("openai", "http://h.example", "sk-mid") for build in built), built
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 3"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"}), resp.text
        assert all(key != "sk-new" for _kind, _url, key in built), built


def test_config_save_racing_a_settings_reload_builds_from_one_snapshot(tmp_path, monkeypatch):
    """裁定 R57 的另一条建法（reload_adapters，PUT 设置 / POST /api/config / 重新确认都经它）：核对与建适配器读同一份
    快照。核对途中 provider 被改成 anthropic + sk-new，建出来的仍是快照里的 openai + sk-old，不是 (openai, sk-new)。"""
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-old"})
    with client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        with _pool(client).lease("w1") as eng:
            built.clear()
            fired = _mutate_inside_the_check(monkeypatch, cfg.providers["p"])
            eng.reload_adapters({"extraction"})
            assert fired
        eng = None
        assert built and all(key != "sk-new" for _kind, _url, key in built), built
        assert ("openai", "http://h.example", "sk-old") in built, built


# ───────────────────────── 裁定 R53：重新确认端点 ─────────────────────────

def test_reconfirm_endpoints_repairs_a_world_after_a_provider_address_change(tmp_path, monkeypatch):
    """p 的 base_url 改了：w1 remember → 409；POST /settings/reconfirm → 200（详情，不含密钥）；remember → 200，适配器
    建在 p 的新地址。默认世界拒绝（invalid_settings，同 PUT 改它的 endpoint）。"""
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-secret-123"})
    with client:
        pool = _pool(client)
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        resp = client.post("/api/config", json={"providers": {"p": {"base_url": "http://h2.example"}}})
        assert resp.status_code == 200, resp.text
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"})
        built.clear()
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "w1"
        assert body["settings"]["extraction"]["provider_name"] == "p"
        assert body["settings"]["extraction"]["base_url"] == "http://h2.example"
        assert body["settings"]["extraction"]["key_set"] is True
        assert "sk-" not in resp.text and "api_key" not in resp.text
        assert pool.registry.get("w1")["settings"]["extraction"]["base_url"] == "http://h2.example"
        assert "api_key" not in str(pool.registry.get("w1")["settings"])
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 3"})
        assert resp.status_code == 200, resp.text
        assert built and all(url == "http://h2.example" for _kind, url, _key in built), built
        resp = client.post("/api/worlds/default/settings/reconfirm")
        assert (resp.status_code, resp.json()) == (400, {"detail": "invalid_settings"})
        resp = client.post("/api/worlds/nope/settings/reconfirm")
        assert (resp.status_code, resp.json()) == (404, {"detail": "world_not_found"})


def test_reconfirm_unbinds_a_role_whose_provider_was_deleted(tmp_path, monkeypatch):
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-secret-123"})
    with client:
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        # 先开着 w1（适配器带着 p 的密钥），再删 p：下一次用到时核对出 provider 不在了。
        assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
        assert client.delete("/api/config/provider/p").status_code == 200
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth 2"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"})
        assert _pool(client).registry.get("w1")["settings"]["extraction"]["provider_name"] == "p"
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        assert resp.status_code == 200, resp.text
        assert "extraction" not in resp.json()["settings"]
        resp = client.post("/api/w/w1/remember", json={"text": "Bob owns auth"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "llm_not_configured"})


# ───────────────────────── 裁定 R58：重新确认端点只在模型或维度变了时重嵌入 ─────────────────────────

def _vector_snapshot(db_path: str) -> list:
    """(stmt_id, embedded_at) 全表，按 stmt_id 排序。重嵌入先删后写，行数可能不变，但 embedded_at 一定变。"""
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT stmt_id, embedded_at FROM statement_vectors "
                           "ORDER BY stmt_id").fetchall()
    finally:
        con.close()


def _embedded_w1(client, monkeypatch) -> tuple:
    """建 w1、写一条、tick 嵌入；返回 (库路径, 向量快照, 记下 _reembed 调用的列表)。"""
    pool = _pool(client)
    assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
    assert client.post("/api/w/w1/remember", json={"text": "Bob owns auth"}).status_code == 200
    assert client.post("/api/w/w1/tick", json={}).status_code == 200
    db = pool.registry.get("w1")["db_path"]
    before = _vector_snapshot(db)
    assert before, "tick did not embed anything"
    reembeds: list = []
    real = DashboardEngine._reembed

    def _spy(self):
        reembeds.append(self.world)
        return real(self)
    monkeypatch.setattr(DashboardEngine, "_reembed", _spy)
    return db, before, reembeds


def test_reconfirm_after_an_address_only_change_keeps_the_vectors(tmp_path, monkeypatch):
    """审查 N-2：p 只换了地址（模型、维度不变）。w1 的嵌入角色过期（endpoint_changed）。以前重新确认经 reload_adapters
    重嵌入：先删光 statement_vectors 再全量重嵌。现在只按新地址重建适配器，向量一行不动。"""
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        db, before, reembeds = _embedded_w1(client, monkeypatch)
        resp = client.post("/api/config", json={"providers": {"p": {"base_url": "http://h2.example"}}})
        assert resp.status_code == 200, resp.text
        reembeds.clear()                                # 默认世界自己的重嵌入不算
        built.clear()
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        assert resp.status_code == 200, resp.text
        assert "warnings" not in resp.json()
        assert reembeds == []
        assert _vector_snapshot(db) == before
        assert built and built[-1]["base_url"] == "http://h2.example", built
        resp = client.post("/api/w/w1/tick", json={})
        assert resp.status_code == 200 and "embed" not in resp.json()["stages_skipped"], resp.text


def test_reconfirm_after_an_embedding_model_change_reembeds(tmp_path, monkeypatch):
    """p 的嵌入模型换了：重新确认把新模型复制进 w1，向量的空间变了，要重嵌入（与设置页换嵌入同一条规则）。"""
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        db, before, reembeds = _embedded_w1(client, monkeypatch)
        resp = client.post("/api/config", json={"providers": {"p": {"model": "m2"}}})
        assert resp.status_code == 200, resp.text
        reembeds.clear()
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        assert resp.status_code == 200, resp.text
        assert reembeds == ["w1"]
        assert _vector_snapshot(db) != before


def test_reconfirm_after_the_embedding_provider_was_deleted_keeps_the_vectors(tmp_path, monkeypatch):
    """p 被删掉：重新确认解绑 w1 的嵌入角色，引擎换成占位嵌入。以前占位嵌入随即把真实向量全删了、重嵌成占位向量；现在
    不重嵌入，向量原样留着（召回降级到重新绑定为止）。"""
    built: list = []
    with _embedding_client(tmp_path, monkeypatch, built) as client:
        db, before, reembeds = _embedded_w1(client, monkeypatch)
        assert client.delete("/api/config/provider/p").status_code == 200
        reembeds.clear()
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        assert resp.status_code == 200, resp.text
        assert "embedding" not in resp.json()["settings"]
        assert reembeds == []
        assert _vector_snapshot(db) == before



def test_reconfirm_copies_the_provider_table_under_the_config_lock(tmp_path, monkeypatch):
    """审查 N-3（裁定 R59）：重新确认拷 provider 表时持 config_lock，与 POST /api/config 互斥。这里在另一个线程里持着
    config_lock，期间把 p 改到一半（只写了新地址），放锁前写完（种类也换掉）。重新确认要等锁：拷到的是改完的条目，
    不是改了一半的。去掉修复后，重新确认不等锁，拷到改了一半的条目（新地址配旧种类）。"""
    import threading
    import time
    built: list = []
    cfg, client = _recording_client(tmp_path, monkeypatch, built,
                                    {"provider": "openai", "model": "m1",
                                     "base_url": "http://h.example", "api_key": "sk-1"})
    with client:
        pool = _pool(client)
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        lock = client.app.state.config_lock
        half = threading.Event()

        def edit():
            with lock:
                cfg.providers["p"]["base_url"] = "http://h2.example"
                half.set()
                time.sleep(0.5)
                cfg.providers["p"]["provider"] = "anthropic"
        editor = threading.Thread(target=edit)
        editor.start()
        assert half.wait(5)
        resp = client.post("/api/worlds/w1/settings/reconfirm")
        editor.join()
        assert resp.status_code == 200, resp.text
        stored = pool.registry.get("w1")["settings"]["extraction"]
        assert (stored["provider"], stored["base_url"]) == ("anthropic", "http://h2.example"), stored

def test_world_list_sorts_by_last_write_then_creation(client):
    """spec §14（审查 M-1）：写过的世界按最近写入降序在前（排序按注册表缓存列），没写过的在后。"""
    pool = _pool(client)
    for name in ("a1", "a2", "a3"):
        assert client.post("/api/worlds", json={"name": name, "labels": {"set": "m1"}}).status_code == 200
    pool.registry.update_stats("a1", 1, 1, "2099-02-01T00:00:00Z", "2099-02-01T00:00:00Z")
    pool.registry.update_stats("a3", 1, 1, "2099-03-01T00:00:00Z", "2099-03-01T00:00:00Z")
    names = [w["name"] for w in client.get("/api/worlds", params={"label.set": "m1"}).json()["worlds"]]
    assert names == ["a3", "a1", "a2"]
