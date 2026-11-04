"""看板 API 按世界寻址（记忆服务第 1 期 Task 6）：/api/w/{world}/... 与旧路径（默认世界）。

建世界的 HTTP 路由（POST /api/worlds）、列表、设置、关闭的用例属于 Task 6b；这里的用例直接经引擎池
建世界、关世界，只测检视与命令路由的寻址和广播。
"""
import pytest
from fastapi.testclient import TestClient

from starling import make_stub_llm
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

    def changed(self, role):
        _core.world_require_endpoint_unchanged(
            {"provider_name": "p", "provider": "openai", "base_url": "https://old.example"},
            "https://new.example")

    monkeypatch.setattr(DashboardEngine, "_require_endpoint_unchanged", changed)
    resp = client.post("/api/w/w1/remember", json={"text": "x"})
    assert (resp.status_code, resp.json()) == (409, {"detail": "endpoint_changed"})


def test_inspecting_a_world_without_a_database_yet(client):
    """建了还没写过的世界没有库文件：检视回 409 world_not_initialized，不替它建引擎、不建库。"""
    pool = _pool(client)
    rec = pool.create_world("fresh", "")
    resp = client.get("/api/w/fresh/statements")
    assert (resp.status_code, resp.json()) == (409, {"detail": "world_not_initialized"})
    assert "fresh" not in pool.open_worlds()
    import os
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
    pool_ref = weakref.ref(app.state.pool)
    if shape == "lifespan":
        with TestClient(app) as test_client:
            _remember(test_client, "/api")
            with test_client.websocket_connect("/ws"):
                pass
            with test_client.websocket_connect("/ws/converse") as ws:
                ws.send_json({"message": ""})
                ws.receive_json()
    else:
        test_client = TestClient(app)
        assert test_client.get("/health").status_code == 200
        assert test_client.post("/api/tick", json={}).status_code == 200
    with app.state.pool._lock:
        engine = app.state.pool._engines.get("default")
        rt_ref = weakref.ref(engine._rt) if engine is not None else None
    engine = None
    if shape == "lifespan":
        assert rt_ref is None                    # 关停已关掉默认世界；Runtime 在下面一并确认已回收
    return pool_ref, rt_ref


@pytest.mark.parametrize("shape", ["plain", "injected", "lifespan"])
def test_discarded_apps_release_pool_and_runtime(tmp_path, monkeypatch, shape):
    import gc
    refs = [_build_and_discard(tmp_path, monkeypatch, index, shape) for index in range(3)]
    gc.collect()
    for pool_ref, rt_ref in refs:
        assert pool_ref() is None, "丢掉的 app 的引擎池还活着"
        if rt_ref is not None:
            assert rt_ref() is None, "丢掉的 app 的默认世界 Runtime 还活着（C++ 写连接没释放）"
