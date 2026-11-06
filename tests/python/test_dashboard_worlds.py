"""世界管理（W1–W4）在引擎池上的行为：/api/worlds 路由、快照、恢复与归档、自动快照（绑定、触发核心、
会话钩子、调度线程）、摄入调用点、租户不变式、lifespan 接线。

记忆服务第 1 期（Task 6b）之后没有「活动世界」：每个世界按需打开、同时开着，激活与切换的用例随切换语义
一起删除（删掉的用例名与原因见提交信息）。恢复只停被恢复的那个世界；只读检视持不建引擎的租约。

设计：docs/superpowers/specs/2026-11-03-memory-service-design.md §5、§7、§8、§13
（快照、回滚、自动快照的语义沿用 docs/superpowers/specs/2026-10-25-world-scene-management-design.md）。
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
from starling.dashboard.world_manager import WorldPool, WorldSwitchError

_STUB = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
         '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
         '"polarity":"POS","nesting_depth":0}]')
_SECONDS_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _code(exc) -> str:
    return _core.world_store_error_code(exc)


def _cfg(tmp_path, **over):
    base = dict(db_path=str(tmp_path / "dashboard.db"), token="", tick_interval_s=0,
                source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"))
    base.update(over)
    return DashboardConfig(**base)


def _fake():
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    return fake


def _stub_factory(ctx, providers):
    """池的引擎工厂：照常建引擎，再换上离线的抽取适配器（不发网络请求）。"""
    engine = DashboardEngine(ctx, providers)
    engine.llm = _fake()
    return engine


class _Clock:
    """可推进的注入时钟：池每次取时刻都调它。"""
    def __init__(self, now: str = "2026-10-28T00:00:00Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


def _pool(tmp_path, cfg=None, **kwargs):
    """一个真注册表上的引擎池（不进 app）。默认世界的设置照 create_app 同步自 config。"""
    cfg = cfg or _cfg(tmp_path)
    reg = _core.WorldRegistry.open(cfg.data_dir, cfg.db_path, cfg.tenant)
    kwargs.setdefault("engine_factory", _stub_factory)
    pool = WorldPool(reg, cfg, **kwargs)
    pool.sync_default_settings()
    pool.lease(pool.default_world()).release()    # 同 lifespan（裁定 R33）：默认世界的库从启动起就在
    return pool, reg


@pytest.fixture
def pool_reg(tmp_path):
    pool, reg = _pool(tmp_path)
    yield pool, reg
    pool.close_all()


def _client(tmp_path, **cfg_over):
    """不进 lifespan 的 app（不起后台线程）。默认世界照 lifespan 那样先打开一次（裁定 R33：生产启动时即建，
    库与 schema 从启动起就在），其余世界按需建。返回 (client, app)。"""
    app = create_app(_cfg(tmp_path, **cfg_over), engine_factory=_stub_factory)
    app.state.pool.lease(app.state.pool.default_world()).release()
    return TestClient(app), app


def _remember(pool, world: str, text: str = "Bob owns auth") -> dict:
    with pool.lease(world) as eng:
        out = eng.remember(text)
        eng = None
    return out


def _count(db_path: str, table: str = "statements") -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _checkpoint(db_path: str) -> None:
    """把 WAL 并回主文件并截成 0 字节：C++ 第 8 步只拒「-wal 非空」，先清空它，测试才能单独验证 Python
    一侧的前提（库上没有还开着的写连接）。"""
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        con.close()
    assert os.path.getsize(db_path + "-wal") == 0


def _insert_foreign_engram(db_path: str, tenant: str) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
        "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
        "VALUES('x-1',?,'hx','conversation','default','inline','normal','audit_retain',"
        "'2026-10-25T00:00:00Z','x-1')", (tenant,))
    con.commit()
    con.close()


def _write_snapshot_write(pool, world: str) -> dict:
    """写 1 条 → 拍快照 → 再写 1 条：快照里 1 条语句，库里 2 条。"""
    _remember(pool, world, "Bob owns auth")
    snap = pool.take_snapshot(world, "before Carol")
    _remember(pool, world, "Carol owns billing")
    return snap


def _db(reg, world: str) -> str:
    return reg.get(world)["db_path"]


def _rollback_outcome(pool, name: str, snapshot_id: str):
    """不用 pytest.raises：要先断言库文件没被替换，再看错误码，回退验证时失败信息才指向「文件被换了」。
    只留错误码，不留异常对象：异常的回溯会钉住池帧里的引擎。"""
    try:
        return pool.rollback(name, snapshot_id), None
    except WorldSwitchError as exc:
        return None, exc.code
    except _core.WorldStoreError as exc:
        return None, _code(exc)


def _rollback_body(world: str, snapshot_id: str) -> dict:
    return {"snapshot_id": snapshot_id, "confirm": world, "mode": "discard_after"}


def _recording(app) -> list:
    sent: list = []

    async def _record(message):
        sent.append(message)
    app.state.ws_manager.broadcast = _record
    return sent


# ───────────────────────── /api/worlds 路由：建、列、删 ─────────────────────────

def test_routes_list_create_delete(tmp_path):
    client, app = _client(tmp_path)
    body = client.get("/api/worlds").json()
    assert body["default"] == "default" and body["snapshot_retention"] == 5
    assert body["total"] == 1 and [w["name"] for w in body["worlds"]] == ["default"]
    assert "active" not in body

    r = client.post("/api/worlds", json={"name": "Bad Name"})
    assert (r.status_code, r.json()["detail"]) == (400, "invalid_world_name")
    row = client.post("/api/worlds", json={"name": "sim-1", "label": "模拟"}).json()
    assert row["name"] == "sim-1" and row["label"] == "模拟" and row["open"] is False
    assert row["tenant"] == "sim-1" and row["statements"] == 0
    r = client.post("/api/worlds", json={"name": "sim-1"})
    assert (r.status_code, r.json()["detail"]) == (409, "world_exists")

    r = client.delete("/api/worlds/sim-1", params={"confirm": "wrong"})
    assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")
    r = client.delete("/api/worlds/default", params={"confirm": "default"})
    assert (r.status_code, r.json()["detail"]) == (409, "world_protected")
    r = client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"})
    assert r.status_code == 200 and r.json() == {"deleted": "sim-1"}
    assert [w["name"] for w in client.get("/api/worlds").json()["worlds"]] == ["default"]
    r = client.delete("/api/worlds/nope", params={"confirm": "nope"})
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    app.state.pool.close_all()


def test_routes_delete_an_open_world_closes_it_first(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    rec = pool.create_world("sim-1", "")
    with pool.lease("sim-1") as eng:
        rt = weakref.ref(eng._rt)
    eng = None
    assert "sim-1" in pool.open_worlds()
    assert client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"}).status_code == 200
    gc.collect()
    assert rt() is None and "sim-1" not in pool.open_worlds()
    assert not os.path.exists(os.path.dirname(rec["db_path"]))
    pool.close_all()


def test_routes_require_token(tmp_path):
    client, app = _client(tmp_path, token="s3cret")
    assert client.get("/api/worlds").status_code == 401
    assert client.get("/api/worlds", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    app.state.pool.close_all()


def test_routes_never_accept_paths(tmp_path):
    client, app = _client(tmp_path)
    # 名称校验本身就挡掉了路径；这里钉住它，防止将来放宽。
    for evil in ("../x", "a/b", "/abs"):
        assert client.post("/api/worlds", json={"name": evil}).status_code in (400, 404, 422)
    assert [w["name"] for w in client.get("/api/worlds").json()["worlds"]] == ["default"]
    app.state.pool.close_all()


@pytest.mark.parametrize("method,path", [
    ("get", "/api/worlds"),
    ("post", "/api/worlds"),
    ("get", "/api/worlds/default"),
    ("delete", "/api/worlds/default?confirm=default"),
    ("put", "/api/worlds/default/settings"),
    ("post", "/api/worlds/default/close"),
    ("get", "/api/worlds/default/snapshots"),
    ("post", "/api/worlds/default/snapshots"),
    ("delete", "/api/worlds/default/snapshots/x"),
    ("post", "/api/worlds/default/rollback"),
    ("delete", "/api/worlds/default/archives?confirm=default"),
    ("get", "/api/worlds/default/sessions"),
    ("put", "/api/worlds/default/auto_snapshot"),
])
def test_routes_503_when_there_is_no_registry(method, path):
    """:memory: 库没有注册表（裁定 R6）：世界管理路由全部回 503 worlds_unavailable，不是 AttributeError。"""
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    assert app.state.pool.registry is None
    bodies = {("post", "/api/worlds"): {"name": "sim-1"},
              ("put", "/api/worlds/default/settings"): {},
              ("post", "/api/worlds/default/rollback"): _rollback_body("default", "s"),
              ("put", "/api/worlds/default/auto_snapshot"): {"on_session": True,
                                                             "interval_hours": 0}}
    kwargs = {"json": bodies[(method, path)]} if (method, path) in bodies else {}
    r = getattr(TestClient(app), method)(path, **kwargs)
    assert (r.status_code, r.json()["detail"]) == (503, "worlds_unavailable")
    app.state.pool.close_all()


def test_routes_list_rows_do_not_open_any_database(tmp_path, monkeypatch):
    """列表只读注册表：关着的世界不只读打开库（几百个世界时逐个打开撑不住），计数取缓存列。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    for name in ("sim-1", "sim-2"):
        pool.create_world(name, "")
    _remember(pool, "sim-1")
    pool.close_world("sim-1")                    # 关闭时写统计缓存
    opened: list = []
    real_stats = _core.world_stats

    def _spy(db_path, tenant):
        opened.append(tenant)
        return real_stats(db_path, tenant)
    monkeypatch.setattr(_core, "world_stats", _spy)
    rows = {w["name"]: w for w in client.get("/api/worlds").json()["worlds"]}
    assert opened == ["default"]                 # 只现读开着的默认世界；关着的世界一个库都没打开
    assert (rows["sim-1"]["statements"], rows["sim-1"]["holders"]) == (1, 1)
    assert rows["sim-1"]["last_write_at"] and rows["sim-2"]["statements"] == 0
    keys = {"name", "label", "labels", "tenant", "created_at", "open", "holders", "statements",
            "last_write_at", "snapshots", "snapshot_bytes", "archives", "archive_bytes",
            "auto_snapshot"}
    assert set(rows["sim-1"]) == keys and set(rows["default"]) == keys
    pool.close_all()


def test_routes_detail_survives_an_unreadable_world_db(tmp_path):
    """库存在但读不出：详情统计置 0、带 stats_error，照常 200；列表不打开库，照常列出；还能删掉。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    db = pool.create_world("sim-1", "")["db_path"]
    with open(db, "wb") as fh:
        fh.write(b"this is not a sqlite database" * 100)
    r = client.get("/api/worlds/sim-1")
    assert r.status_code == 200
    detail = r.json()
    assert detail["stats_error"] is True
    assert (detail["db_bytes"], detail["live_bytes"], detail["engrams"]) == (0, 0, 0)
    assert client.get("/api/worlds/default").json().get("stats_error", False) is False
    assert client.get("/api/worlds").status_code == 200
    assert client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"}).status_code == 200
    pool.close_all()


def test_routes_non_domain_exception_is_json_500_internal_error(tmp_path, monkeypatch, caplog):
    client, app = _client(tmp_path)

    def _raise(*_a, **_k):
        raise OSError("disk exploded at /secret/path")
    monkeypatch.setattr(app.state.pool, "take_snapshot", _raise)
    with caplog.at_level("ERROR"):
        r = client.post("/api/worlds/default/snapshots", json={})
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "internal_error"}            # 不把异常文本（含路径）泄给客户端
    assert "disk exploded" in caplog.text                       # 服务端日志里有
    app.state.pool.close_all()


def test_routes_unknown_error_code_falls_back_to_500(tmp_path, monkeypatch):
    client, app = _client(tmp_path)

    def _raise(*_a, **_k):
        raise WorldSwitchError("something_new")
    monkeypatch.setattr(app.state.pool, "take_snapshot", _raise)
    r = client.post("/api/worlds/default/snapshots", json={})
    assert (r.status_code, r.json()["detail"]) == (500, "something_new")
    app.state.pool.close_all()


def test_routes_delete_refuses_a_world_dir_aliased_to_a_sibling_world_dir(tmp_path):
    """worlds/<sim-1> -> worlds/<sim-2>：删 sim-1 不能解析到 sim-2、删掉它的目录和库。C++ 拒绝
    （snapshot_failed，路由回 500），sim-2 一个字节都不动。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    sim1_home = os.path.dirname(pool.create_world("sim-1", "")["db_path"])
    sim2_db = pool.create_world("sim-2", "")["db_path"]
    sim2_home = os.path.dirname(sim2_db)
    _insert_foreign_engram(sim2_db, "sim-2")
    for name in os.listdir(sim1_home):
        path = os.path.join(sim1_home, name)
        os.rmdir(path) if os.path.isdir(path) else os.remove(path)
    os.rmdir(sim1_home)
    os.symlink(sim2_home, sim1_home)
    before = sorted(os.listdir(sim2_home))
    r = client.delete("/api/worlds/sim-1", params={"confirm": "sim-1"})
    assert (r.status_code, r.json()["detail"]) == (500, "snapshot_failed")
    assert sorted(os.listdir(sim2_home)) == before
    assert _count(sim2_db, "engrams") == 1 and os.path.islink(sim1_home)
    pool.close_all()


# ───────────────────────── 快照路由 ─────────────────────────

def test_routes_snapshots(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    _remember(pool, "default")
    snap = client.post("/api/worlds/default/snapshots", json={"note": "n1"}).json()
    assert snap["kind"] == "manual" and snap["note"] == "n1" and snap["size_bytes"] > 0
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    assert [s["id"] for s in listed] == [snap["id"]]
    row = client.get("/api/worlds").json()["worlds"][0]
    assert row["snapshots"] == 1 and row["snapshot_bytes"] == snap["size_bytes"]
    assert client.delete(f"/api/worlds/default/snapshots/{snap['id']}").status_code == 200
    r = client.delete("/api/worlds/default/snapshots/missing")
    assert (r.status_code, r.json()["detail"]) == (404, "snapshot_not_found")
    r = client.post("/api/worlds/nope/snapshots", json={})
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    pool.close_all()


def test_routes_snapshot_of_a_closed_world_does_not_open_it(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    pool.create_world("sim-1", "")
    _remember(pool, "sim-1")
    pool.close_world("sim-1")
    snap = client.post("/api/worlds/sim-1/snapshots", json={}).json()
    assert snap["size_bytes"] > 0 and _count(pool.registry.get("sim-1")["db_path"]) == 1
    assert "sim-1" not in pool.open_worlds()
    pool.close_all()


def test_routes_snapshot_refused_for_zvec(tmp_path):
    client, app = _client(tmp_path)
    app.state.config.vector_backend = "zvec"      # 默认世界的向量后端取 config（裁定 R26）
    r = client.post("/api/worlds/default/snapshots", json={})
    assert (r.status_code, r.json()["detail"]) == (409, "snapshot_unsupported_backend")
    assert app.state.pool.snapshots("default") == []
    app.state.pool.close_all()


def test_routes_snapshot_rows_hide_server_paths_and_sort_descending(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    _remember(pool, "default")
    first = client.post("/api/worlds/default/snapshots", json={"note": "a"}).json()
    second = client.post("/api/worlds/default/snapshots", json={"note": "b"}).json()
    assert set(first) == {"id", "kind", "reason", "note", "created_at", "world_time", "size_bytes",
                          "sessions_complete", "session_id"}
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    stamps = [s["created_at"] for s in listed]
    assert stamps == sorted(stamps, reverse=True)
    assert {s["id"] for s in listed} == {first["id"], second["id"]}
    row = client.get("/api/worlds").json()["worlds"][0]
    assert row["snapshots"] == 2
    assert row["snapshot_bytes"] == first["size_bytes"] + second["size_bytes"]
    pool.close_all()


def test_routes_snapshot_body_is_optional(tmp_path):
    client, app = _client(tmp_path)
    r = client.post("/api/worlds/default/snapshots")          # 无 body
    assert r.status_code == 200 and r.json()["note"] == ""
    app.state.pool.close_all()


def test_routes_snapshot_insufficient_disk_is_507(tmp_path, monkeypatch):
    client, app = _client(tmp_path)

    def _no_space(*_args, **_kwargs):
        # C++ 的 free_space 钩子没有暴露给 Python，没法真造出磁盘不足；这里在绑定边界抛出同形异常
        # （what() 形如 "<code>: <detail>"），只验证路由的 507 映射。
        raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
    monkeypatch.setattr(_core, "world_take_snapshot", _no_space)
    r = client.post("/api/worlds/default/snapshots", json={})
    assert (r.status_code, r.json()["detail"]) == (507, "insufficient_disk")
    monkeypatch.undo()
    assert client.get("/api/worlds/default/snapshots").json()["snapshots"] == []
    app.state.pool.close_all()


def test_routes_report_missing_snapshot_file_and_allow_delete(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    os.remove(pool.snapshots("default")[0]["path"])
    listed = client.get("/api/worlds/default/snapshots").json()["snapshots"]
    assert len(listed) == 1
    assert listed[0]["missing"] is True and listed[0]["size_bytes"] == 0
    assert client.get("/api/worlds").json()["worlds"][0]["snapshot_bytes"] == 0
    assert client.delete(f"/api/worlds/default/snapshots/{snap['id']}").status_code == 200
    assert client.get("/api/worlds/default/snapshots").json() == {"snapshots": []}
    pool.close_all()


def test_routes_list_snapshots_unknown_world_is_404(tmp_path):
    client, app = _client(tmp_path)
    r = client.get("/api/worlds/nope/snapshots")
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    assert client.get("/api/worlds/default/snapshots").json() == {"snapshots": []}
    app.state.pool.close_all()


def test_routes_broadcast_snapshot_created_with_world(tmp_path):
    app = create_app(_cfg(tmp_path), engine_factory=_stub_factory)
    with TestClient(app) as client:
        client.post("/api/worlds", json={"name": "sim-1"})
        with client.websocket_connect("/ws") as ws:
            snap = client.post("/api/worlds/sim-1/snapshots", json={}).json()
            client.portal.call(app.state.ws_manager.broadcast, {"type": "sentinel"})
            frames = []
            while (msg := ws.receive_json())["type"] != "sentinel":
                frames.append(msg)
    assert {"type": "snapshot_created", "world": "sim-1",
            "payload": {"world": "sim-1", "id": snap["id"]}} in frames


def test_routes_failed_operations_do_not_broadcast(tmp_path):
    client, app = _client(tmp_path)
    sent = _recording(app)
    assert client.post("/api/worlds/nope/snapshots", json={}).status_code == 404
    r = client.post("/api/worlds/nope/rollback", json=_rollback_body("nope", "x"))
    assert r.status_code == 404
    assert sent == []
    app.state.pool.close_all()


def test_concurrent_snapshots_of_one_world_do_not_lose_entries(pool_reg, tmp_path):
    """C++ 要求按世界串行化快照：池持世界锁，并发的六份都登记、文件都在。"""
    pool, reg = pool_reg
    sqlite3.connect(os.path.join(reg.data_dir, "registry.db")).execute(
        "UPDATE meta SET value='20' WHERE key='snapshot_retention'").connection.commit()
    errors: list = []

    def _snap():
        try:
            pool.take_snapshot("default", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=_snap) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert errors == []
    listed = pool.snapshots("default")
    assert len(listed) == 6 and len({s["id"] for s in listed}) == 6
    assert all(os.path.exists(s["path"]) for s in listed)


def test_snapshot_sweeps_crash_leftovers(pool_reg):
    pool, _reg = pool_reg
    first = pool.take_snapshot("default", "")
    snap_dir = os.path.dirname(first["path"])
    partial = os.path.join(snap_dir, "20260101T000000Z-dead.db.partial")
    orphan = os.path.join(snap_dir, "20260101T000000Z-beef.db")
    for path in (partial, orphan):
        with open(path, "w") as fh:
            fh.write("x")
    second = pool.take_snapshot("default", "")
    assert not os.path.exists(partial) and not os.path.exists(orphan)
    assert os.path.exists(first["path"]) and os.path.exists(second["path"])


# ───────────────────────── 租户不变式（裁定 R36） ─────────────────────────

def test_binding_verify_tenant(tmp_path):
    db = str(tmp_path / "w.db")
    _core.world_verify_tenant(db, "sim-1")                          # 库不存在：新世界，放行
    _core.SqliteAdapter.open(db)
    _core.world_verify_tenant(db, "sim-1")
    _insert_foreign_engram(db, "intruder")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_verify_tenant(db, "sim-1")
    assert _code(ei.value) == "world_tenant_mismatch" and "engrams" in str(ei.value)


def test_first_open_refuses_a_world_with_foreign_tenant_rows(tmp_path):
    """非默认世界第一次建引擎之前，C++ 核对库里只有它自己的租户：命令路由回 409 world_tenant_mismatch，
    不建引擎、不发布；别的世界照常服务。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    rec = pool.create_world("sim-1", "")
    _insert_foreign_engram(rec["db_path"], "intruder")
    built: list = []
    real = pool._open_engine
    pool._open_engine = lambda world, ctx: built.append(world) or real(world, ctx)
    for method, path, body in (("post", "/api/w/sim-1/tick", {}),
                               ("post", "/api/w/sim-1/remember", {"text": "x"})):
        r = client.request(method, path, json=body)
        assert (r.status_code, r.json()) == (409, {"detail": "world_tenant_mismatch"}), path
    assert built == [] and "sim-1" not in pool.open_worlds()
    assert client.post("/api/tick", json={}).status_code == 200      # 默认世界照常
    pool.close_all()


def test_first_open_of_a_clean_world_still_works(pool_reg):
    pool, _reg = pool_reg
    pool.create_world("sim-1", "")
    out = _remember(pool, "sim-1")
    assert out["outcome"] == "accepted" and "sim-1" in pool.open_worlds()


def test_default_world_is_not_tenant_checked(tmp_path):
    """默认世界（原地 dashboard.db）早于世界管理，既有部署可按租户过滤地存多个租户：打开它不因此拒绝。"""
    cfg = _cfg(tmp_path)
    DashboardEngine(cfg).close()                        # 建库
    gc.collect()
    _insert_foreign_engram(cfg.db_path, "other")
    pool, _reg = _pool(tmp_path, cfg)
    try:
        assert _remember(pool, "default")["outcome"] == "accepted"
    finally:
        pool.close_all()


def test_world_whose_db_cannot_be_read_is_world_open_failed(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    db = pool.create_world("sim-1", "")["db_path"]
    os.remove(db)
    os.makedirs(db)                                     # 库路径是个目录：只读扫描打不开
    r = client.post("/api/w/sim-1/tick", json={})
    assert (r.status_code, r.json()["detail"]) == (500, "world_open_failed")
    assert "sim-1" not in pool.open_worlds()
    pool.close_all()


# ───────────────────────── 恢复（W3，只停被恢复的世界） ─────────────────────────

def test_binding_rollback_roundtrip_archives_and_error_codes(pool_reg):
    _pool_, reg = pool_reg
    db = reg.create("sim-1", "", {}, None, "2026-10-25T01:00:00Z")["db_path"]
    _core.SqliteAdapter.open(db)                  # 跑迁移建库（create 只登记，不建库）
    gc.collect()
    snap = _core.world_take_snapshot(reg, "sim-1")["snapshot"]
    _insert_foreign_engram(db, "sim-1")           # 快照之后的写入（本租户）
    assert _count(db, "engrams") == 1
    result = _core.world_rollback(reg, "sim-1", snap["id"])
    assert set(result) == {"archive_path", "restored_bytes", "digest"}
    assert result["restored_bytes"] == snap["size_bytes"]
    home = os.path.dirname(reg.get("sim-1")["snapshot_dir"])
    assert os.path.dirname(result["archive_path"]) == os.path.join(home, "archive")
    assert _count(db, "engrams") == 0                                  # 回到快照那一刻
    con = sqlite3.connect(f"file:{result['archive_path']}?mode=ro&immutable=1", uri=True)
    assert con.execute("SELECT COUNT(*) FROM engrams").fetchone()[0] == 1   # 旧库原样在归档里
    con.close()
    listed = _core.world_list_archives(reg, "sim-1")
    assert listed == [{"name": os.path.basename(result["archive_path"]),
                       "size_bytes": os.path.getsize(result["archive_path"])}]
    assert _core.world_purge_archives(reg, "sim-1") == 1
    assert _core.world_list_archives(reg, "sim-1") == []
    for call, code in (
            (lambda: _core.world_rollback(reg, "sim-1", "nope"), "snapshot_not_found"),
            (lambda: _core.world_rollback(reg, "nope", snap["id"]), "world_not_found"),
            (lambda: _core.world_rollback(reg, "sim-1", snap["id"], vector_backend="zvec"),
             "snapshot_unsupported_backend"),
            (lambda: _core.world_list_archives(reg, "nope"), "world_not_found"),
            (lambda: _core.world_purge_archives(reg, "nope"), "world_not_found")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _code(ei.value) == code


def test_binding_check_rollback_codes_and_touches_nothing(pool_reg):
    pool, reg = pool_reg
    db = pool.create_world("sim-1", "")["db_path"]
    snap = _core.world_take_snapshot(reg, "sim-1")["snapshot"]
    _insert_foreign_engram(db, "sim-1")
    with open(db, "rb") as fh:
        before = fh.read()
    listing = sorted(os.listdir(os.path.dirname(db)))
    assert _core.world_check_rollback(reg, "sim-1", snap["id"]) is None   # 有效快照：通过
    for call, code in (
            (lambda: _core.world_check_rollback(reg, "nope", snap["id"]), "world_not_found"),
            (lambda: _core.world_check_rollback(reg, "sim-1", "nope"), "snapshot_not_found"),
            (lambda: _core.world_check_rollback(reg, "sim-1", snap["id"], vector_backend="zvec"),
             "snapshot_unsupported_backend")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _code(ei.value) == code
    os.remove(snap["path"])
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_check_rollback(reg, "sim-1", snap["id"])
    assert _code(ei.value) == "snapshot_corrupt"
    with open(db, "rb") as fh:
        assert fh.read() == before
    assert sorted(os.listdir(os.path.dirname(db))) == listing      # 没建 archive/、没留 partial


_READER = (
    "import sqlite3, sys\n"
    "con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)\n"
    "print(con.execute('SELECT COUNT(*) FROM engrams').fetchone()[0], flush=True)\n"
    "sys.stdin.read()\n"
    "con.close()\n")


def test_binding_rollback_refuses_db_open_in_another_process(pool_reg):
    pool, reg = pool_reg
    db = pool.create_world("sim-1", "")["db_path"]
    snap = _core.world_take_snapshot(reg, "sim-1")["snapshot"]
    _insert_foreign_engram(db, "sim-1")
    proc = subprocess.Popen([sys.executable, "-I", "-c", _READER, db], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "1"          # 另一个进程读过了，连接开着
        with open(db, "rb") as fh:
            before = fh.read()
        inode = os.stat(db).st_ino
        with pytest.raises(_core.WorldStoreError) as ei:
            _core.world_rollback(reg, "sim-1", snap["id"])
        assert _code(ei.value) == "world_in_use"
        assert os.stat(db).st_ino == inode
        with open(db, "rb") as fh:
            assert fh.read() == before
        assert _core.world_list_archives(reg, "sim-1") == []
    finally:
        proc.stdin.close()
        proc.wait(10)
    result = _core.world_rollback(reg, "sim-1", snap["id"])    # 进程退出之后照常回滚
    assert result["archive_path"] and _count(db, "engrams") == 0


def test_rollback_closed_world_restores_snapshot_and_keeps_it_closed(pool_reg):
    pool, reg = pool_reg
    pool.create_world("sim-1", "")
    snap = _write_snapshot_write(pool, "sim-1")
    pool.close_world("sim-1")
    db = _db(reg, "sim-1")
    assert _count(db) == 2
    out = pool.rollback("sim-1", snap["id"])
    assert out == {"world": "sim-1", "snapshot_id": snap["id"], "archived": True,
                   "reopened": False}
    assert _count(db) == 1 and "sim-1" not in pool.open_worlds()
    archives = pool.archives("sim-1")
    assert len(archives) == 1 and archives[0]["size_bytes"] > 0
    _remember(pool, "sim-1", "Dave owns search")             # 恢复出来的库能正常打开、写入
    assert _count(db) == 2


def test_rollback_refreshes_the_closed_worlds_cached_count(pool_reg):
    """审查 T5-1：关着的世界回滚之后，列表（注册表缓存列）立刻显示回滚后的计数，不是回滚前关闭时写的那份。"""
    pool, reg = pool_reg
    pool.create_world("sim-1", "")
    snap = _write_snapshot_write(pool, "sim-1")
    pool.close_world("sim-1")
    assert reg.get("sim-1")["statements"] == 2               # 关闭时写的缓存
    pool.rollback("sim-1", snap["id"])
    assert "sim-1" not in pool.open_worlds()
    assert reg.get("sim-1")["statements"] == 1
    rows = {w["name"]: w for w in pool.list_worlds({}, 0, 50)["worlds"]}
    assert rows["sim-1"]["statements"] == 1


def test_rollback_open_world_rebuilds_engine_and_restarts_workers(tmp_path):
    pool, reg = _pool(tmp_path, _cfg(tmp_path, tick_interval_s=60.0))
    try:
        pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                              scheduler_first_delay_s=3600.0)
        snap = _write_snapshot_write(pool, "default")
        db = _db(reg, "default")
        with pool.lease("default", build=False) as old:
            old_rt = weakref.ref(old._rt)
            old_threads = (old._tick_thread, old._ingest_thread)
        old = None
        assert all(t is not None and t.is_alive() for t in old_threads)
        inode = os.stat(db).st_ino
        out = pool.rollback("default", snap["id"])
        assert out == {"world": "default", "snapshot_id": snap["id"], "archived": True,
                       "reopened": True}
        gc.collect()
        assert old_rt() is None, "旧 Runtime 必须在动文件之前已被回收"
        assert all(not t.is_alive() for t in old_threads)
        assert os.stat(db).st_ino != inode and _count(db) == 1
        with pool.lease("default", build=False) as new:
            assert new is not None
            assert new._tick_thread is not None and new._tick_thread.is_alive()
            assert new._ingest_thread is not None and new._ingest_thread.is_alive()
        new = None
        _remember(pool, "default", "Dave owns search")       # 回滚后照常写入
        assert _count(db) == 2 and len(pool.archives("default")) == 1
    finally:
        pool.close_all()


def test_rollback_with_corrupt_snapshot_reopens_the_untouched_db(pool_reg):
    pool, reg = pool_reg
    snap = _write_snapshot_write(pool, "default")
    db = _db(reg, "default")
    with open(snap["path"], "wb") as fh:
        fh.write(b"this is not a sqlite database" * 512)
    inode = os.stat(db).st_ino
    out, code = _rollback_outcome(pool, "default", snap["id"])
    assert out is None and code == "snapshot_corrupt"             # C++ 的码（预检）原样抛出
    assert os.stat(db).st_ino == inode and _count(db) == 2        # 目标库未动
    assert pool.archives("default") == [] and "default" in pool.open_worlds()
    _remember(pool, "default", "Dave owns search")
    assert _count(db) == 3


@pytest.mark.parametrize("cpp_fails", [False, True])
def test_rollback_reopen_failure_code_says_whether_the_db_changed(tmp_path, cpp_fails):
    """恢复之后重建引擎失败：引擎不在表上；下一次租约重新建。码按库有没有被替换来分：替换成功之后是
    rollback_reopen_failed（库已是快照内容）；C++ 在替换之前失败（world_in_use）之后是 world_open_failed
    （链到 C++ 的错误，库未动）。"""
    pool, reg = _pool(tmp_path)
    try:
        snap = _write_snapshot_write(pool, "default")
        db = _db(reg, "default")
        _checkpoint(db)
        real = pool._factory
        pool._factory = lambda ctx, providers: (_ for _ in ()).throw(RuntimeError("disk on fire"))
        reader = None
        if cpp_fails:
            reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)    # 不在租约里、读过库
            assert reader.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 2
        try:
            with pytest.raises(WorldSwitchError) as ei:
                pool.rollback("default", snap["id"])
            code, detail, cause = ei.value.code, ei.value.detail, ei.value.__cause__
            del ei
        finally:
            if reader is not None:
                reader.close()
        assert "disk on fire" in detail and "default" not in pool.open_worlds()
        if cpp_fails:
            assert code == "world_open_failed"
            assert isinstance(cause, _core.WorldStoreError) and _code(cause) == "world_in_use"
            assert _count(db) == 2
        else:
            assert code == "rollback_reopen_failed" and "rollback applied" in detail
            assert _count(db) == 1
        del cause
        pool._factory = real
        assert _remember(pool, "default", "Dave owns search")["outcome"] == "accepted"
    finally:
        pool.close_all()


def test_rollback_refuses_while_the_old_runtime_is_still_referenced(tmp_path, monkeypatch, caplog):
    """被恢复的世界丢了引擎、旧 Runtime 却回收不了（别处钉着它，写连接还开着）：world_busy，库文件不动；
    此后也不在同一个库上开第二条写连接。回收之后照常恢复。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_RUNTIME_RELEASE_GRACE_S", 0.1)
    pool, reg = _pool(tmp_path)
    try:
        snap = _write_snapshot_write(pool, "default")
        db = _db(reg, "default")
        with pool.lease("default", build=False) as eng:
            keep = eng._rt                   # 故意钉住旧 Runtime：它的写连接一直开着
        eng = None
        _checkpoint(db)
        inode = os.stat(db).st_ino
        with caplog.at_level("WARNING"):
            out, code = _rollback_outcome(pool, "default", snap["id"])
        assert os.stat(db).st_ino == inode, "旧 Runtime 还开着写连接，库文件却被替换了"
        assert out is None and code == "world_busy"
        assert "still referenced" in caplog.text
        assert _count(db) == 2 and pool.archives("default") == []
        with pytest.raises(WorldSwitchError) as ei:
            pool.lease("default")
        assert ei.value.code == "world_busy"
        del ei
        del keep
        gc.collect()
        out = pool.rollback("default", snap["id"])
        assert out["archived"] is True and _count(db) == 1
    finally:
        pool.close_all()


def test_rollback_times_out_on_a_stuck_lease_and_keeps_the_engine(tmp_path, monkeypatch):
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.3)
    pool, reg = _pool(tmp_path, _cfg(tmp_path, tick_interval_s=60.0))
    try:
        pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                              scheduler_first_delay_s=3600.0)
        snap = _write_snapshot_write(pool, "default")
        db = _db(reg, "default")
        inode = os.stat(db).st_ino
        held = pool.lease("default")
        eng_id = id(held.engine)
        try:
            out, code = _rollback_outcome(pool, "default", snap["id"])
            assert out is None and code == "world_busy"
            assert os.stat(db).st_ino == inode and _count(db) == 2      # 文件未动
            assert pool.archives("default") == [] and pool.closing_worlds() == []
            eng = held.engine
            assert id(eng) == eng_id                                    # 引擎没丢
            assert eng._tick_thread is not None and eng._tick_thread.is_alive()
            assert eng._ingest_thread is not None and eng._ingest_thread.is_alive()
            eng = None
        finally:
            held.release()
    finally:
        pool.close_all()


def test_rollback_refuses_when_workers_will_not_stop(pool_reg, monkeypatch):
    """quiesce 失败（后台线程 5 秒内没停下）：world_busy，库未动，引擎原样保留。"""
    pool, reg = pool_reg
    snap = _write_snapshot_write(pool, "default")
    db = _db(reg, "default")
    with pool.lease("default", build=False) as eng:
        monkeypatch.setattr(eng, "quiesce", lambda timeout_s=5.0: False)
        eng.quiesce_stuck = ("tick",)
        eng_id = id(eng)
    eng = None
    inode = os.stat(db).st_ino
    with pytest.raises(WorldSwitchError) as ei:
        pool.rollback("default", snap["id"])
    code, detail = ei.value.code, ei.value.detail
    del ei
    assert os.stat(db).st_ino == inode
    assert code == "world_busy" and "tick" in detail and "ingest" not in detail   # 点名卡住的线程
    with pool.lease("default", build=False) as again:
        assert id(again) == eng_id
    again = None
    assert _count(db) == 2 and pool.archives("default") == []


def test_rollback_waits_for_an_in_flight_inspect_query(tmp_path, monkeypatch):
    """检视 SQL 路由持不建引擎的租约直到连接关闭：一条打开了旧 inode、还没读的只读连接（C++ 的
    world_in_use 探测看不见它）在飞时，恢复等不到租约归零 → world_busy，库未动。它结束之后恢复照常成功。"""
    import starling.dashboard.world_manager as wm
    monkeypatch.setattr(wm, "_LEASE_DRAIN_TIMEOUT_S", 0.3)
    client, app = _client(tmp_path)
    pool = app.state.pool
    snap = _write_snapshot_write(pool, "default")
    db = _db(pool.registry, "default")
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
        out, code = _rollback_outcome(pool, "default", snap["id"])
        assert os.stat(db).st_ino == inode, "检视查询还开着库，库文件却被替换了"
        assert out is None and code == "world_busy"
        assert pool.closing_worlds() == [] and "default" in pool.open_worlds()
    finally:
        release.set()
        req.join(10)
    assert results and results[0].status_code == 200
    out = pool.rollback("default", snap["id"])
    assert out["reopened"] is True and os.stat(db).st_ino != inode and _count(db) == 1
    pool.close_all()


def test_rollback_with_a_db_open_outside_any_lease_reopens_the_untouched_db(pool_reg):
    """不在租约里的连接读过库：C++ 的 world_in_use 原样抛出，按注册表重建引擎，库未动。"""
    pool, reg = pool_reg
    snap = _write_snapshot_write(pool, "default")
    db = _db(reg, "default")
    _checkpoint(db)
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert reader.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 2
        inode = os.stat(db).st_ino
        out, code = _rollback_outcome(pool, "default", snap["id"])
        assert os.stat(db).st_ino == inode
        assert out is None and code == "world_in_use"
        assert "default" in pool.open_worlds() and pool.archives("default") == []
        _remember(pool, "default", "Dave owns search")
        assert _count(db) == 3
    finally:
        reader.close()
    out = pool.rollback("default", snap["id"])
    assert out["reopened"] is True and _count(db) == 1


def test_rollback_refuses_zvec_backend_before_touching_the_engine(pool_reg, monkeypatch):
    """拒绝由 C++ 给出（预检，在停引擎之前）：引擎不丢、不重建，库与归档都不动。"""
    pool, reg = pool_reg
    snap = _write_snapshot_write(pool, "default")
    db = _db(reg, "default")
    with pool.lease("default", build=False) as eng:
        eng_id = id(eng)
    eng = None
    inode = os.stat(db).st_ino
    pool._config.vector_backend = "zvec"
    out, code = _rollback_outcome(pool, "default", snap["id"])
    assert out is None and code == "snapshot_unsupported_backend"
    with pool.lease("default", build=False) as again:
        assert id(again) == eng_id
    again = None
    assert os.stat(db).st_ino == inode and _count(db) == 2 and pool.archives("default") == []
    pool._config.vector_backend = "sqlite"


def test_rollback_unknown_world_is_world_not_found(pool_reg):
    pool, _reg = pool_reg
    with pytest.raises(WorldSwitchError) as ei:
        pool.rollback("nope", "x")
    assert ei.value.code == "world_not_found"


def test_routes_rollback_200_broadcast_and_archives(tmp_path):
    app = create_app(_cfg(tmp_path), engine_factory=_stub_factory)
    pool = app.state.pool
    with TestClient(app) as client:
        _remember(pool, "default", "Bob owns auth")
        snap = client.post("/api/worlds/default/snapshots", json={}).json()
        _remember(pool, "default", "Carol owns billing")
        with client.websocket_connect("/ws") as ws:
            r = client.post("/api/worlds/default/rollback",
                            json=_rollback_body("default", snap["id"]))
            assert r.status_code == 200, r.json()
            assert r.json() == {"world": "default", "snapshot_id": snap["id"],
                                "archived": True, "reopened": True}
            client.portal.call(app.state.ws_manager.broadcast, {"type": "sentinel"})
            frames = []
            while (msg := ws.receive_json())["type"] != "sentinel":
                frames.append(msg)
        assert {"type": "world_rolled_back", "world": "default",
                "payload": {"world": "default", "snapshot_id": snap["id"]}} in frames
        assert client.get("/api/overview").json()["counts"]["statements"] == 1
        row = client.get("/api/worlds").json()["worlds"][0]
        assert row["archives"] == 1 and row["archive_bytes"] > 0
        r = client.delete("/api/worlds/default/archives", params={"confirm": "default"})
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        row = client.get("/api/worlds").json()["worlds"][0]
        assert (row["archives"], row["archive_bytes"]) == (0, 0)
        r = client.delete("/api/worlds/nope/archives", params={"confirm": "nope"})
        assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")


def test_routes_rollback_mode_defaults_to_discard_after(tmp_path):
    client, app = _client(tmp_path)
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    r = client.post("/api/worlds/default/rollback",
                    json={"snapshot_id": snap["id"], "confirm": "default"})
    assert r.status_code == 200 and r.json()["archived"] is True
    app.state.pool.close_all()


def test_routes_rollback_and_purge_check_confirm_and_mode_before_the_pool(tmp_path, monkeypatch):
    client, app = _client(tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("pool must not be called")
    monkeypatch.setattr(app.state.pool, "rollback", _boom)
    monkeypatch.setattr(app.state.pool, "purge_archives", _boom)
    monkeypatch.setattr(app.state.pool, "delete_world", _boom)
    url = "/api/worlds/default/rollback"
    for body in ({"snapshot_id": "x"}, {"snapshot_id": "x", "confirm": "other"}):
        r = client.post(url, json=body)
        assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")
    r = client.post(url, json={"snapshot_id": "x", "confirm": "default", "mode": "scene"})
    assert (r.status_code, r.json()["detail"]) == (400, "rollback_mode_unsupported")
    for params in ({}, {"confirm": "other"}):
        r = client.delete("/api/worlds/default/archives", params=params)
        assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")
        r = client.delete("/api/worlds/sim-1", params=params)
        assert (r.status_code, r.json()["detail"]) == (400, "confirm_mismatch")
    monkeypatch.undo()
    app.state.pool.close_all()


def test_routes_rollback_not_found_codes(tmp_path):
    client, app = _client(tmp_path)
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", "missing"))
    assert (r.status_code, r.json()["detail"]) == (404, "snapshot_not_found")
    r = client.post("/api/worlds/nope/rollback", json=_rollback_body("nope", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    assert client.post("/api/tick", json={}).status_code == 200      # 引擎照常可用
    app.state.pool.close_all()


def test_routes_rollback_to_a_missing_snapshot_file_is_409_snapshot_corrupt(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    _remember(pool, "default", "Bob owns auth")
    snap = client.post("/api/worlds/default/snapshots", json={}).json()
    _remember(pool, "default", "Carol owns billing")
    os.remove(pool.snapshots("default")[0]["path"])
    db = _db(pool.registry, "default")
    inode = os.stat(db).st_ino
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (409, "snapshot_corrupt")
    assert os.stat(db).st_ino == inode and _count(db) == 2
    assert client.post("/api/tick", json={}).status_code == 200
    pool.close_all()


@pytest.mark.parametrize("source,code,status", [
    ("cpp", "snapshot_corrupt", 409), ("cpp", "world_tenant_mismatch", 409),
    ("cpp", "insufficient_disk", 507), ("cpp", "snapshot_failed", 500),
    ("cpp", "snapshot_unsupported_backend", 409), ("cpp", "world_in_use", 409),
    ("pool", "world_busy", 503), ("pool", "world_open_failed", 500),
])
def test_routes_rollback_maps_error_codes_and_does_not_broadcast(tmp_path, monkeypatch,
                                                                source, code, status):
    client, app = _client(tmp_path)
    sent = _recording(app)

    def _raise(_name, _snapshot_id):
        if source == "cpp":
            raise _core.WorldStoreError(f"{code}: x")     # 与 C++ what() 同形
        raise WorldSwitchError(code, "x")
    monkeypatch.setattr(app.state.pool, "rollback", _raise)
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", "s"))
    assert (r.status_code, r.json()["detail"]) == (status, code)
    assert sent == []
    monkeypatch.undo()
    app.state.pool.close_all()


def test_routes_rollback_reopen_failure_after_replace_is_500_and_broadcasts(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    sent = _recording(app)
    snap = _write_snapshot_write(pool, "default")
    snap_path = pool.snapshots("default")[0]["path"]
    pool._factory = lambda ctx, providers: (_ for _ in ()).throw(RuntimeError("disk on fire"))
    r = client.post("/api/worlds/default/rollback", json=_rollback_body("default", snap["id"]))
    assert (r.status_code, r.json()["detail"]) == (500, "rollback_reopen_failed")
    assert sent == [{"type": "world_rolled_back", "world": "default",
                     "payload": {"world": "default", "snapshot_id": snap["id"]}}]
    db = _db(pool.registry, "default")
    with open(db, "rb") as live, open(snap_path, "rb") as frozen:
        assert live.read() == frozen.read()                       # 库就是快照的内容
    assert "default" not in pool.open_worlds() and _count(db) == 1
    pool._factory = _stub_factory
    assert client.post("/api/tick", json={}).status_code == 200  # 下一次租约重新建
    pool.close_all()


def test_routes_rollback_preflight_refusal_keeps_the_live_engine(tmp_path, monkeypatch):
    """开着的世界上，不改输入重试也过不了的请求（不存在的快照 id、快照文件不在）在停引擎之前就被 C++
    预检拒掉：同一个引擎对象、后台线程从没被停、关闭窗口从没打开、库文件 inode 不变。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    snap = _write_snapshot_write(pool, "default")
    db = _db(pool.registry, "default")
    inode = os.stat(db).st_ino
    quiesced: list = []
    real_quiesce = DashboardEngine.quiesce
    with pool.lease("default", build=False) as eng:
        eng_id = id(eng)
    eng = None
    # 在类上打桩，不在实例上存绑定方法（绑定方法引用实例，关闭时就等不到旧 Runtime 回收）。
    monkeypatch.setattr(DashboardEngine, "quiesce",
                        lambda self, *a, **k: quiesced.append(True) or real_quiesce(self, *a, **k))
    for snapshot_id, status, code in (("20990101T000000Z-dead", 404, "snapshot_not_found"),
                                      (snap["id"], 409, "snapshot_corrupt")):
        if code == "snapshot_corrupt":
            os.remove(snap["path"])
        r = client.post("/api/worlds/default/rollback",
                        json=_rollback_body("default", snapshot_id))
        assert (r.status_code, r.json()["detail"]) == (status, code)
    monkeypatch.undo()
    with pool.lease("default", build=False) as again:
        assert id(again) == eng_id, "预检失败却拆掉了引擎"
    again = None
    assert quiesced == [], "预检失败却停了后台线程"
    assert os.stat(db).st_ino == inode and _count(db) == 2 and pool.archives("default") == []
    pool.close_all()


def test_routes_created_world_row_carries_zero_archives(tmp_path):
    client, app = _client(tmp_path)
    row = client.post("/api/worlds", json={"name": "sim-1"}).json()
    assert (row["archives"], row["archive_bytes"]) == (0, 0)
    app.state.pool.close_all()


def test_routes_unreadable_archive_dir_is_zero_archives_not_an_error(tmp_path, caplog):
    client, app = _client(tmp_path)
    pool = app.state.pool
    home = os.path.dirname(pool.registry.get("default")["snapshot_dir"])
    os.makedirs(home, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(home, "archive"))   # 解析后越出世界 home：C++ 拒列
    with caplog.at_level("WARNING"):
        r = client.get("/api/worlds")
    assert r.status_code == 200
    row = r.json()["worlds"][0]
    assert (row["archives"], row["archive_bytes"]) == (0, 0)
    assert "world_list_archives failed" in caplog.text
    pool.close_all()


# ───────────────────────── 自动快照（W4）：绑定 ─────────────────────────

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


def _auto_snaps(pool, world: str = "default") -> list:
    return [s for s in pool.snapshots(world) if s["kind"] == "auto"]


def test_binding_auto_snapshot_config_roundtrip(pool_reg):
    pool, reg = pool_reg
    auto = lambda world: _core.world_effective_settings(  # noqa: E731
        reg.get(world)["settings"])["background"]["auto_snapshot"]
    assert auto("default") == {"on_session": False, "interval_hours": 24}
    reg.create("sim-1", "", {}, None, "2026-10-28T00:00:00Z")
    assert auto("sim-1") == {"on_session": False, "interval_hours": 24}     # 继承默认世界
    reg.create("sim-2", "", {}, {"background": {"auto_snapshot": {
        "on_session": False, "interval_hours": 12}}}, "2026-10-28T00:00:00Z")
    assert auto("sim-2") == {"on_session": False, "interval_hours": 12}
    _core.world_set_auto_snapshot(reg, "sim-1", False, 6)
    assert auto("sim-1") == {"on_session": False, "interval_hours": 6}
    assert auto("default") == {"on_session": False, "interval_hours": 24}    # 只改了 sim-1
    # 设置的其余部分原样保留。
    assert reg.get("sim-1")["settings"]["background"].get("tick_interval_s") == \
        reg.get("default")["settings"]["background"].get("tick_interval_s")


@pytest.mark.parametrize("on_session, hours", [
    (True, 8761), (True, -1), (1, 6), (True, True), (True, 6.0), (True, "6"), (True, 2 ** 70)])
def test_binding_set_auto_snapshot_rejects_bad_config(pool_reg, on_session, hours):
    pool, reg = pool_reg
    before = reg.get("default")["settings"]
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_set_auto_snapshot(reg, "default", on_session, hours)
    assert _code(ei.value) == "invalid_auto_snapshot"
    assert reg.get("default")["settings"] == before
    # 建世界时同一份配置经设置合并进 C++：越界与类型错都拒，什么都不建。
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.create("sim-1", "", {}, {"background": {"auto_snapshot": {
            "on_session": on_session, "interval_hours": hours}}}, "2026-10-28T00:00:00Z")
    assert _code(ei.value) in ("invalid_auto_snapshot", "invalid_settings")
    with pytest.raises(_core.WorldStoreError):
        reg.get("sim-1")
    worlds_dir = os.path.join(reg.data_dir, "worlds")
    left = set(os.listdir(worlds_dir)) if os.path.isdir(worlds_dir) else set()
    assert left <= {reg.get("default")["dir_id"]}, "拒绝的世界留下了目录"


def test_binding_set_auto_snapshot_unknown_world(pool_reg):
    _pool_, reg = pool_reg
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_set_auto_snapshot(reg, "nope", True, 0)
    assert _code(ei.value) == "world_not_found"


def test_binding_snapshot_dict_carries_w4_fields(pool_reg):
    pool, reg = pool_reg
    db = pool.create_world("sim-1", "")["db_path"]
    _core.world_set_auto_snapshot(reg, "sim-1", True, 0)   # 新世界继承默认世界：按会话关着
    adapter = _core.SqliteAdapter.open(db)
    _retain_session(adapter, "sim-1", "s-old")
    manual = _core.world_take_snapshot(reg, "sim-1", note="手动")["snapshot"]
    assert manual["sessions_complete"] == 1 and manual["session_id"] is None
    assert isinstance(manual["content_mark"], str) and manual["content_mark"]

    out = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z", "s-new")
    snap = out["snapshot"]
    assert out["skipped"] is None and out["refused"] is None
    assert (snap["kind"], snap["reason"], snap["session_id"]) == ("auto", "session", "s-new")
    assert snap["created_at"] == "2026-10-28T08:00:00Z"
    assert snap["id"].startswith("20261028T080000Z-")
    assert snap["sessions_complete"] == 1 and snap["content_mark"] == manual["content_mark"]
    assert reg.snapshots("sim-1")[0]["id"] == snap["id"]          # C++ 已登记

    again = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:05:00Z", "s-new")
    assert again["snapshot"] is None and again["skipped"] == "unchanged"
    known = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:05:00Z", "s-old")
    assert known["snapshot"] is None and known["skipped"] == "known_session"
    zvec = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:05:00Z", "s-x",
                                          vector_backend="zvec")
    assert zvec["snapshot"] is None and zvec["skipped"] == "unsupported_backend"
    del adapter
    gc.collect()


def test_binding_take_auto_snapshot_rejects_microsecond_now(pool_reg):
    """C++ 只认秒精度：isoformat 的那种时刻会被拒。"""
    _pool_, reg = pool_reg
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_take_auto_snapshot(reg, "default", "interval", "2026-10-28T08:00:00.123456Z")
    assert _code(ei.value) == "invalid_auto_snapshot"


def test_binding_list_sessions_shape(pool_reg):
    pool, reg = pool_reg
    db = pool.create_world("sim-1", "")["db_path"]
    assert _core.world_list_sessions(reg, "sim-1", 200) == {
        "total": 0, "null_observed": 0, "truncated": False, "sessions": []}
    _core.world_set_auto_snapshot(reg, "sim-1", True, 0)
    adapter = _core.SqliteAdapter.open(db)
    _retain_session(adapter, "sim-1", "s-0")           # 有内容，按会话的快照才不是 empty
    anchor = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z",
                                            "s-1")["snapshot"]
    _retain_session(adapter, "sim-1", "s-1")
    _retain_session(adapter, "sim-1", "s-2")
    listed = _core.world_list_sessions(reg, "sim-1", 1)
    assert (listed["total"], listed["null_observed"], listed["truncated"]) == (3, 0, True)
    assert len(listed["sessions"]) == 1
    full = _core.world_list_sessions(reg, "sim-1", 200)
    rows = {row["session_id"]: row for row in full["sessions"]}
    assert set(rows) == {"s-0", "s-1", "s-2"} and full["truncated"] is False
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


def test_binding_take_auto_snapshot_refused_shape(tmp_path):
    """修剪卡住：refused 是原因字符串、snapshot 与 skipped 为 None；部分修剪 C++ 已落库。"""
    pool, reg = _pool(tmp_path)
    try:
        normal, stuck = _stuck_retention(pool, reg)
        _core.world_set_auto_snapshot(reg, "default", True, 0)
        out = _core.world_take_auto_snapshot(reg, "default", "session", "2026-10-28T08:00:00Z", "s-1")
        assert out["snapshot"] is None and out["skipped"] is None
        assert out["refused"].startswith("retention cannot prune")
        ids = [s["id"] for s in reg.snapshots("default")]
        assert normal not in ids and stuck in ids
        skipped = _core.world_take_auto_snapshot(reg, "default", "session",
                                                 "2026-10-28T08:00:00Z", "s-x",
                                                 vector_backend="zvec")
        assert skipped["refused"] is None and skipped["skipped"] == "unsupported_backend"
    finally:
        pool.close_all()


# ───────────────────────── 自动快照：池上的触发核心 ─────────────────────────

def _hold_world_lock(pool, world: str = "default"):
    """另一个线程持住该世界的世界锁，直到返回的 Event 被 set。"""
    held, release = threading.Event(), threading.Event()

    def _holder():
        with pool._world_lock(world):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(10)
    return holder, release


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


def _world_lock_is_free(pool, world: str = "default") -> bool:
    """从另一个线程非阻塞取一次世界锁，取到就放开。RLock 可重入，在拿过锁的同一线程上看不出泄漏。"""
    lock = pool._world_lock(world)

    def _probe() -> bool:
        if lock.acquire(blocking=False):
            lock.release()
            return True
        return False
    prober, result = _call_in_thread(_probe)
    assert not prober.is_alive()
    return result.get("value") is True


def _stuck_retention(pool, reg) -> tuple[str, str]:
    """修剪的真实卡住场景：默认世界拍 4 份手动快照（m1 最旧），保留份数调到 2，把第二旧的那份（m2）换成
    指向自己的符号链接（解析时 ELOOP，修剪删不掉它）。修剪从最旧起：先删掉正常的 m1，再卡在 m2 上。"""
    _remember(pool, "default")
    ids = [pool.take_snapshot("default", f"m{index}")["id"] for index in range(1, 5)]
    con = sqlite3.connect(os.path.join(reg.data_dir, "registry.db"))
    con.execute("UPDATE meta SET value='2' WHERE key='snapshot_retention'")
    con.commit()
    con.close()
    stuck = next(s for s in reg.snapshots("default") if s["id"] == ids[1])["path"]
    os.remove(stuck)
    os.symlink(os.path.basename(stuck), stuck)
    return ids[0], ids[1]


def test_auto_snapshot_status_starts_clean(pool_reg):
    pool, _reg = pool_reg
    assert pool.auto_snapshot_status() == {"failures": 0, "last_error": None, "last_error_at": None}


def test_auto_snapshot_interval_follows_the_injected_clock(tmp_path):
    clock = _Clock("2026-10-28T00:00:00Z")
    pool, _reg = _pool(tmp_path, clock=clock)
    try:
        _remember(pool, "default", "Bob owns auth")
        first = pool._auto_snapshot("default", "interval")
        assert first.startswith("taken:"), first
        snaps = _auto_snaps(pool)
        assert [s["id"] for s in snaps] == [first.split(":", 1)[1]]
        assert (snaps[0]["reason"], snaps[0]["created_at"]) == ("interval", "2026-10-28T00:00:00Z")
        assert os.path.isfile(snaps[0]["path"])
        clock.now = "2026-10-28T23:59:59Z"
        assert pool._auto_snapshot("default", "interval") == "skipped:not_due"
        clock.now = "2026-10-29T00:00:00Z"
        assert pool._auto_snapshot("default", "interval") == "skipped:unchanged"
        _remember(pool, "default", "Carol owns billing")
        assert pool._auto_snapshot("default", "interval").startswith("taken:")
        assert [s["created_at"] for s in _auto_snaps(pool)] == ["2026-10-29T00:00:00Z",
                                                                "2026-10-28T00:00:00Z"]
        assert pool.auto_snapshot_status()["failures"] == 0
    finally:
        pool.close_all()


def test_auto_snapshot_default_clock_is_seconds_precision(pool_reg):
    """不注入时钟时，交给 C++ 的必须是秒精度（YYYY-MM-DDTHH:MM:SSZ），否则每一轮都会计为失败。"""
    pool, _reg = pool_reg
    _remember(pool, "default")
    outcome = pool._auto_snapshot("default", "interval")
    assert outcome.startswith("taken:"), (outcome, pool.auto_snapshot_status())
    assert _SECONDS_ISO.fullmatch(_auto_snaps(pool)[0]["created_at"])


def test_auto_snapshot_cpp_failure_is_counted_not_raised(tmp_path, monkeypatch, caplog):
    clock = _Clock("2026-10-28T00:00:00Z")
    pool, _reg = _pool(tmp_path, clock=clock)
    try:
        def _no_space(*_args, **_kwargs):
            raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
        monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
        with caplog.at_level("WARNING", logger="starling.dashboard.world_manager"):
            assert pool._auto_snapshot("default", "interval") == "failed"
            clock.now = "2026-10-28T01:00:00Z"
            assert pool._auto_snapshot("default", "session", "s-1") == "failed"
        assert pool.auto_snapshot_status() == {"failures": 2, "last_error": "insufficient_disk",
                                               "last_error_at": "2026-10-28T01:00:00Z"}
        assert "insufficient_disk" in caplog.text and _auto_snaps(pool) == []
        assert _world_lock_is_free(pool), "failed 之后世界锁必须已放开"
    finally:
        pool.close_all()


def test_auto_snapshot_non_domain_failure_is_internal_error(tmp_path, monkeypatch):
    clock = _Clock("2026-10-28T00:00:00Z")
    pool, _reg = _pool(tmp_path, clock=clock)
    try:
        def _boom(*_args, **_kwargs):
            raise OSError("registry write failed")
        monkeypatch.setattr(_core, "world_take_auto_snapshot", _boom)
        assert pool._auto_snapshot("default", "interval") == "failed"
        assert pool.auto_snapshot_status() == {"failures": 1, "last_error": "internal_error",
                                               "last_error_at": "2026-10-28T00:00:00Z"}
        assert _world_lock_is_free(pool)
    finally:
        pool.close_all()


def test_auto_snapshot_zvec_is_a_skip_not_a_failure(tmp_path):
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        pool._config.vector_backend = "zvec"
        assert pool._auto_snapshot("default", "interval") == "skipped:unsupported_backend"
        assert pool.auto_snapshot_status()["failures"] == 0 and _auto_snaps(pool) == []
        pool._config.vector_backend = "sqlite"
    finally:
        pool.close_all()


def test_auto_snapshot_is_busy_without_waiting_when_the_world_lock_is_held(tmp_path):
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        pool.set_auto_snapshot("default", True, 24)
        hook = pool._make_session_hook("default")
        holder, release = _hold_world_lock(pool)
        try:
            caller, result = _call_in_thread(pool._auto_snapshot, "default", "interval")
            assert not caller.is_alive(), "后台调用方在世界锁上等了"
            assert result == {"value": "busy"}
            hooked, hook_result = _call_in_thread(hook, "s-1")
            assert not hooked.is_alive(), "会话钩子在世界锁上等了"
            assert isinstance(hook_result.get("error"), SessionSnapshotDeferred), hook_result
        finally:
            release.set()
            holder.join(10)
        assert _world_lock_is_free(pool), "busy 之后世界锁必须已放开"
        assert _auto_snaps(pool) == [] and pool.auto_snapshot_status()["failures"] == 0
        assert pool._auto_snapshot("default", "interval").startswith("taken:")
    finally:
        pool.close_all()


def test_auto_snapshot_of_one_world_is_not_blocked_by_another_worlds_lock(tmp_path):
    """世界锁按世界：w1 正在恢复或快照（持着它的世界锁）时，默认世界的自动快照照常拍。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        pool.create_world("w1", "")
        _remember(pool, "default")
        holder, release = _hold_world_lock(pool, "w1")
        try:
            assert pool._auto_snapshot("default", "interval").startswith("taken:")
            assert pool._auto_snapshot("w1", "interval") == "busy"
        finally:
            release.set()
            holder.join(10)
    finally:
        pool.close_all()


def test_refused_auto_snapshot_counts_snapshot_failed_and_keeps_the_partial_prune(tmp_path):
    pool, reg = _pool(tmp_path, clock=_Clock())
    try:
        normal, stuck = _stuck_retention(pool, reg)
        pool.set_auto_snapshot("default", True, 1)
        assert pool._auto_snapshot("default", "session", "s-1") == "failed"
        saved = [s["id"] for s in reg.snapshots("default")]
        assert normal not in saved and stuck in saved and len(saved) == 3
        assert _auto_snaps(pool) == []
        status = pool.auto_snapshot_status()
        assert (status["failures"], status["last_error"]) == (1, "snapshot_failed")
        assert pool._auto_snapshot("default", "interval") == "failed"
        assert [s["id"] for s in reg.snapshots("default")] == saved
        assert pool.auto_snapshot_status()["failures"] == 2
        assert _world_lock_is_free(pool)
    finally:
        pool.close_all()


# ── 会话钩子 ──

def test_session_hook_snapshots_a_new_session_once(tmp_path):
    clock = _Clock("2026-10-28T00:00:00Z")
    pool, _reg = _pool(tmp_path, clock=clock)
    try:
        _remember(pool, "default")
        pool.set_auto_snapshot("default", True, 0)
        hook = pool._make_session_hook("default")
        assert hook("s-1") is None
        snaps = _auto_snaps(pool)
        assert [(s["reason"], s["session_id"]) for s in snaps] == [("session", "s-1")]
        assert _count(snaps[0]["path"], "source_unit_meta") == 0     # 快照里还没有这个会话
        with pool.lease("default") as eng:
            _retain_session(eng._rt.adapter, eng.tenant, "s-1")
        eng = None
        clock.now = "2026-10-28T00:05:00Z"
        assert pool._auto_snapshot("default", "session", "s-1") == "skipped:known_session"
        assert hook("s-1") is None
        assert len(_auto_snaps(pool)) == 1                           # 同一会话第二次不拍
    finally:
        pool.close_all()


def test_session_hook_retry_with_unchanged_content_is_a_normal_skip(tmp_path):
    """写入失败、会话一直是新的时，同一会话、同一标记不再拍；钩子照常返回。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        pool.set_auto_snapshot("default", True, 0)
        hook = pool._make_session_hook("default")
        assert hook("s-2") is None
        assert pool._auto_snapshot("default", "session", "s-2") == "skipped:unchanged"
        assert hook("s-2") is None
        assert len(_auto_snaps(pool)) == 1
    finally:
        pool.close_all()


def test_session_hook_failure_returns_normally(tmp_path, monkeypatch):
    """用户裁定：快照失败照常摄入，钩子不抛。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        pool.set_auto_snapshot("default", True, 0)

        def _no_space(*_args, **_kwargs):
            raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
        monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
        assert pool._make_session_hook("default")("s-1") is None
        assert pool.auto_snapshot_status()["failures"] == 1
    finally:
        pool.close_all()


@pytest.mark.parametrize("on_session", [False, True], ids=["on_session_off", "on_session_on"])
def test_session_hook_defers_only_when_the_world_snapshots_per_session(tmp_path, on_session):
    """按会话关着（默认世界的默认配置）时，钩子取锁之前就返回：世界锁被别的线程占着也不推迟摄入。
    开着时取不到锁就抛 SessionSnapshotDeferred。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        pool.set_auto_snapshot("default", on_session, 24)
        hook = pool._make_session_hook("default")
        holder, release = _hold_world_lock(pool)
        try:
            hooked, result = _call_in_thread(hook, "s-1")
            assert not hooked.is_alive(), "会话钩子在世界锁上等了"
        finally:
            release.set()
            holder.join(10)
        if on_session:
            assert isinstance(result.get("error"), SessionSnapshotDeferred), result
        else:
            assert result == {"value": None}, result
        assert _auto_snaps(pool) == [] and pool.auto_snapshot_status()["failures"] == 0
    finally:
        pool.close_all()


def test_session_hook_falls_through_to_the_locked_path_when_the_lock_free_read_fails(
        tmp_path, monkeypatch):
    """锁外那次读注册表失败不算失败，也不当作关着跳过，而是走持锁路径：锁被占就照样推迟；锁空着就照常拍。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        pool.set_auto_snapshot("default", True, 0)
        failing = {"next": 0}
        real_effective = _core.world_effective_settings

        def _effective_failing_next(settings):
            if failing["next"] > 0:
                failing["next"] -= 1
                raise _core.WorldStoreError("registry_corrupt: transient read error")
            return real_effective(settings)
        hook = pool._make_session_hook("default")
        monkeypatch.setattr(_core, "world_effective_settings", _effective_failing_next)
        failing["next"] = 1
        holder, release = _hold_world_lock(pool)
        try:
            hooked, result = _call_in_thread(hook, "s-1")
            assert not hooked.is_alive(), "会话钩子在世界锁上等了"
        finally:
            release.set()
            holder.join(10)
        assert failing["next"] == 0, "锁外那次读没有发生"
        assert isinstance(result.get("error"), SessionSnapshotDeferred), result
        failing["next"] = 1
        assert hook("s-1") is None
        assert [s["session_id"] for s in _auto_snaps(pool)] == ["s-1"]
        assert pool.auto_snapshot_status()["failures"] == 0
    finally:
        pool.close_all()


def test_session_hook_swallows_a_failure_in_recording_the_failure(tmp_path, monkeypatch, caplog):
    """记失败本身也出错时，钩子的最外层兜底记 exception、照常返回；世界锁照样放开。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        pool.set_auto_snapshot("default", True, 0)

        def _no_space(*_args, **_kwargs):
            raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")

        def _broken_record(*_args, **_kwargs):
            raise RuntimeError("logging handler exploded")
        monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
        monkeypatch.setattr(pool, "_count_auto_failure", _broken_record)
        with caplog.at_level("ERROR", logger="starling.dashboard.world_manager"):
            assert pool._make_session_hook("default")("s-1") is None
        assert "session auto snapshot hook failed" in caplog.text
        assert "logging handler exploded" in caplog.text
        assert _world_lock_is_free(pool)
    finally:
        pool.close_all()


def test_every_engine_the_pool_builds_carries_the_session_hook(pool_reg):
    pool, _reg = pool_reg
    pool.create_world("w1", "")
    pool.set_auto_snapshot("w1", True, 0)
    with pool.lease("w1") as eng:
        assert eng.session_snapshot_hook is not None
        _remember(pool, "w1")
        assert eng.session_snapshot_hook("s-9") is None
    eng = None
    assert [s["session_id"] for s in _auto_snaps(pool, "w1")] == ["s-9"]
    assert _auto_snaps(pool, "default") == []                 # 钩子绑着自己的世界


# ── 按时间的调度线程 ──

def _scheduler_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "starling-auto-snapshot" and t.is_alive()]


class _Rounds:
    """把调度线程的每一轮变成测试可控的一步：线程每轮先等 step() 放行，再跑真的一轮，结果放进队列。
    close() 之后放行的轮次不再调真方法，关后台才不会等一轮快照。"""

    def __init__(self, pool, monkeypatch) -> None:
        self._go = threading.Semaphore(0)
        self._closed = False
        self.results: queue.Queue = queue.Queue()
        self.thread_names: set = set()
        real = pool.auto_snapshot_round

        def _spy(stop=None):
            if self._closed or not self._go.acquire(timeout=30) or self._closed:
                return {}
            self.thread_names.add(threading.current_thread().name)
            out = real(stop)
            self.results.put(out)
            return out
        monkeypatch.setattr(pool, "auto_snapshot_round", _spy)

    def step(self, timeout: float = 10.0) -> dict:
        self._go.release()
        return self.results.get(timeout=timeout)

    def close(self) -> None:
        self._closed = True
        self._go.release()


def test_scheduler_rounds_run_on_their_own_thread(tmp_path, monkeypatch):
    clock = _Clock("2026-10-28T00:00:00Z")
    pool, _reg = _pool(tmp_path, clock=clock)
    try:
        _remember(pool, "default")
        rounds = _Rounds(pool, monkeypatch)
        before = set(_scheduler_threads())
        pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=0.01,
                              scheduler_first_delay_s=0)
        started = [t for t in _scheduler_threads() if t not in before]
        try:
            assert len(started) == 1 and started[0].daemon
            assert rounds.step()["default"].startswith("taken:")
            assert rounds.step()["default"] == "skipped:not_due"      # 时钟没推进
            clock.now = "2026-10-29T01:00:00Z"
            _remember(pool, "default", "Carol owns billing")
            assert rounds.step()["default"].startswith("taken:")
            assert rounds.thread_names == {"starling-auto-snapshot"}
            assert [(s["reason"], s["created_at"]) for s in _auto_snaps(pool)] == [
                ("interval", "2026-10-29T01:00:00Z"), ("interval", "2026-10-28T00:00:00Z")]
            assert started[0].is_alive() and _world_lock_is_free(pool)
        finally:
            rounds.close()
            pool.stop_background()
        assert all(not t.is_alive() for t in started)
    finally:
        pool.close_all()


def test_scheduler_skips_a_busy_world_and_takes_it_next_round(tmp_path, monkeypatch):
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        _remember(pool, "default")
        rounds = _Rounds(pool, monkeypatch)
        pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=0.01,
                              scheduler_first_delay_s=0)
        try:
            holder, release = _hold_world_lock(pool)
            try:
                assert rounds.step()["default"] == "busy"
            finally:
                release.set()
                holder.join(10)
            assert _auto_snaps(pool) == []
            assert rounds.step()["default"].startswith("taken:")
            assert pool.auto_snapshot_status()["failures"] == 0
        finally:
            rounds.close()
            pool.stop_background()
    finally:
        pool.close_all()


def test_stop_background_joins_the_scheduler_and_is_idempotent(pool_reg):
    pool, _reg = pool_reg
    before = set(_scheduler_threads())
    pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                          scheduler_first_delay_s=3600.0)
    pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                          scheduler_first_delay_s=3600.0)              # 已开着：no-op
    started = [t for t in _scheduler_threads() if t not in before]
    assert len(started) == 1
    pool.stop_background()
    assert not started[0].is_alive()
    pool.stop_background()                                            # 再停一次是 no-op
    assert [t for t in _scheduler_threads() if t not in before] == []


def test_close_all_also_stops_the_scheduler(tmp_path):
    pool, _reg = _pool(tmp_path)
    before = set(_scheduler_threads())
    pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                          scheduler_first_delay_s=3600.0)
    started = [t for t in _scheduler_threads() if t not in before]
    assert len(started) == 1
    pool.close_all()
    assert not started[0].is_alive()


# ───────────────────────── 自动快照路由 ─────────────────────────

def test_routes_put_auto_snapshot_writes_and_rejects(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    r = client.put("/api/worlds/default/auto_snapshot", json={"on_session": True, "interval_hours": 6})
    assert r.status_code == 200
    assert r.json() == {"world": "default", "auto_snapshot": {"on_session": True, "interval_hours": 6}}
    row = client.get("/api/worlds").json()["worlds"][0]
    assert row["auto_snapshot"] == {"on_session": True, "interval_hours": 6}
    before = pool.registry.get("default")["settings"]
    for hours in (8761, -1, 2 ** 70):
        r = client.put("/api/worlds/default/auto_snapshot",
                       json={"on_session": False, "interval_hours": hours})
        assert (r.status_code, r.json()["detail"]) == (400, "invalid_auto_snapshot"), hours
    r = client.put("/api/worlds/nope/auto_snapshot", json={"on_session": True, "interval_hours": 0})
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    assert client.put("/api/worlds/default/auto_snapshot", json={"on_session": True}).status_code == 422
    assert pool.registry.get("default")["settings"] == before
    pool.close_all()


def test_routes_auto_snapshot_body_is_strictly_typed(tmp_path):
    """宽松模式会把 "yes" 转成 True、true 转成 1、5.0 转成 5。严格类型下这些都回 422，注册表不变；PUT 与新建
    世界带的 auto_snapshot 是同一个模型，两处都钉住。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    before = pool.registry.get("default")["settings"]
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
    assert pool.registry.get("default")["settings"] == before
    assert client.get("/api/worlds/sim-strict").status_code == 404
    r = client.post("/api/worlds", json={"name": "sim-strict",
                                         "auto_snapshot": {"on_session": True, "interval_hours": 0}})
    assert r.status_code == 200, r.json()
    assert r.json()["auto_snapshot"] == {"on_session": True, "interval_hours": 0}
    pool.close_all()


def test_routes_create_world_with_and_without_auto_snapshot(tmp_path):
    client, app = _client(tmp_path)
    plain = client.post("/api/worlds", json={"name": "sim-1"}).json()
    # 没给就继承默认世界当时的设置（spec §8.2）。
    assert plain["auto_snapshot"] == {"on_session": False, "interval_hours": 24}
    custom = client.post("/api/worlds", json={"name": "sim-2", "label": "x",
                                              "auto_snapshot": {"on_session": False,
                                                                "interval_hours": 12}}).json()
    assert custom["auto_snapshot"] == {"on_session": False, "interval_hours": 12}
    r = client.post("/api/worlds", json={"name": "sim-3",
                                         "auto_snapshot": {"on_session": True, "interval_hours": 9000}})
    assert (r.status_code, r.json()["detail"]) == (400, "invalid_auto_snapshot")
    assert client.get("/api/worlds/sim-3").status_code == 404
    rows = {w["name"]: w for w in client.get("/api/worlds").json()["worlds"]}
    assert rows["default"]["auto_snapshot"] == {"on_session": False, "interval_hours": 24}
    app.state.pool.close_all()


def test_routes_list_worlds_carries_auto_snapshot_status(tmp_path):
    client, app = _client(tmp_path)
    body = client.get("/api/worlds").json()
    assert set(body) == {"default", "total", "snapshot_retention", "auto_snapshot_status",
                         "worlds"}
    assert body["auto_snapshot_status"] == {"failures": 0, "last_error": None, "last_error_at": None}
    app.state.pool.close_all()


def _sim_world_with_sessions(pool) -> dict:
    """在关着的世界 sim-1 里写三个会话（s-1 先有一份按会话快照作锚点，s-null 的 observed_at 全为 NULL），
    返回锚点快照。测试自己开一条写连接再关掉。"""
    reg = pool.registry
    db = pool.create_world("sim-1", "")["db_path"]
    pool.set_auto_snapshot("sim-1", True, 0)
    adapter = _core.SqliteAdapter.open(db)
    _retain_session(adapter, "sim-1", "s-0")
    anchor = _core.world_take_auto_snapshot(reg, "sim-1", "session", "2026-10-28T08:00:00Z",
                                            "s-1")["snapshot"]
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
    return anchor


def test_routes_sessions_shape_anchor_and_null_times(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    anchor = _sim_world_with_sessions(pool)
    r = client.get("/api/worlds/sim-1/sessions")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"world", "source_lane_enabled", "total", "null_observed", "truncated",
                         "sessions"}
    assert (body["world"], body["source_lane_enabled"]) == ("sim-1", False)   # 继承默认世界：关着
    assert (body["total"], body["null_observed"], body["truncated"]) == (4, 2, False)
    rows = {row["session_id"]: row for row in body["sessions"]}
    assert rows["s-1"] == {"session_id": "s-1", "units": 2, "speakers": 2,
                           "first_at": "2026-10-27T09:00:00Z", "last_at": "2026-10-27T09:00:00Z",
                           "anchor": {"snapshot_id": anchor["id"],
                                      "created_at": "2026-10-28T08:00:00Z"}}
    assert rows["s-2"]["anchor"] is None
    assert (rows["s-null"]["first_at"], rows["s-null"]["last_at"]) == (None, None)
    limited = client.get("/api/worlds/sim-1/sessions", params={"limit": 1}).json()
    assert (limited["total"], limited["truncated"], len(limited["sessions"])) == (4, True, 1)
    # source_lane_enabled 取这个世界自己的设置。
    pool.update_settings("sim-1", {"source_lane": {"enabled": True}})
    assert client.get("/api/worlds/sim-1/sessions").json()["source_lane_enabled"] is True
    assert "sim-1" not in pool.open_worlds()                      # 列会话不打开世界
    pool.close_all()


def test_routes_sessions_maps_empty_cpp_strings_to_null(tmp_path, monkeypatch):
    """C++ 的 SessionRow 没有锚点、没有时间时是空串；路由按契约给 null，不依赖绑定已经转好。"""
    client, app = _client(tmp_path)

    def _raw(_reg, _world, _limit):
        return {"total": 1, "null_observed": 2, "truncated": False,
                "sessions": [{"session_id": "s-x", "units": 2, "speakers": 1, "first_at": "",
                              "last_at": "", "anchor": {"snapshot_id": "", "created_at": ""},
                              "extra": "dropped"}]}
    monkeypatch.setattr(_core, "world_list_sessions", _raw)
    body = client.get("/api/worlds/default/sessions").json()
    assert body["sessions"] == [{"session_id": "s-x", "units": 2, "speakers": 1, "first_at": None,
                                 "last_at": None, "anchor": None}]
    monkeypatch.undo()
    app.state.pool.close_all()


def test_routes_sessions_limit_404_and_503(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    assert client.get("/api/worlds/default/sessions").json()["sessions"] == []
    assert client.get("/api/worlds/default/sessions", params={"limit": 1000}).status_code == 200
    for limit in (0, 1001, -5):
        # FastAPI 在 C++ 之前回 422：越界的 limit 不该显示成「自动快照设置无效」。
        assert client.get("/api/worlds/default/sessions",
                          params={"limit": limit}).status_code == 422, limit
    r = client.get("/api/worlds/nope/sessions")
    assert (r.status_code, r.json()["detail"]) == (404, "world_not_found")
    with pool._lock:
        pool._closing.add("default")
    try:
        r = client.get("/api/worlds/default/sessions")
        assert (r.status_code, r.json()["detail"]) == (503, "world_busy")
    finally:
        with pool._lock:
            pool._closing.discard("default")
    pool.close_all()


def test_routes_snapshot_rows_carry_w4_fields_and_never_content_mark(tmp_path):
    client, app = _client(tmp_path)
    pool = app.state.pool
    _remember(pool, "default")                                    # 有内容，内容标记才非空
    manual = client.post("/api/worlds/default/snapshots", json={"note": "m"})
    assert "content_mark" not in manual.text
    assert (manual.json()["sessions_complete"], manual.json()["session_id"]) == (0, None)
    pool.set_auto_snapshot("default", True, 0)
    assert pool._make_session_hook("default")("s-1") is None
    listed = client.get("/api/worlds/default/snapshots")
    assert "content_mark" not in listed.text
    rows = {row["reason"]: row for row in listed.json()["snapshots"]}
    assert (rows["session"]["kind"], rows["session"]["session_id"]) == ("auto", "s-1")
    assert rows["manual"]["session_id"] is None
    assert "content_mark" not in client.get("/api/worlds").text
    assert all("content_mark" in snap for snap in pool.snapshots("default"))   # 只在内部
    pool.close_all()


def test_routes_list_a_pre_w4_snapshot_entry_with_null_w4_fields(tmp_path):
    """从 worlds.json 导入的 W4 之前的条目没有 sessions_complete / session_id / content_mark：列表照常列出，两个
    字段回 null（不能 KeyError 成 500）。"""
    client, app = _client(tmp_path)
    pool = app.state.pool
    snap = pool.take_snapshot("default", "w3")
    con = sqlite3.connect(os.path.join(pool.registry.data_dir, "registry.db"))
    con.execute("UPDATE snapshots SET sessions_complete=NULL, session_id='', content_mark='' "
                "WHERE id=?", (snap["id"],))
    con.commit()
    con.close()
    r = client.get("/api/worlds/default/snapshots")
    assert r.status_code == 200
    (row,) = r.json()["snapshots"]
    assert row["id"] == snap["id"]
    assert (row["sessions_complete"], row["session_id"]) == (None, None)
    assert "content_mark" not in row
    assert client.get("/api/worlds").status_code == 200
    pool.close_all()


def test_snapshot_row_of_a_pre_w4_entry_has_null_w4_fields(tmp_path):
    from starling.dashboard.routes.worlds import _snapshot_row
    legacy = {"id": "20261025T011200Z-a1b2", "kind": "auto", "reason": "session", "note": "",
              "created_at": "2026-10-25T01:12:00Z", "world_time": "", "size_bytes": 10,
              "path": str(tmp_path / "gone.db")}
    row = _snapshot_row(legacy)
    assert (row["sessions_complete"], row["session_id"]) == (None, None)
    assert "content_mark" not in row and "path" not in row


# ───────────────────────── 摄入调用点（W4 T3） ─────────────────────────

def _user_line(text: str, ts: str = "2026-10-27T09:00:00.000Z") -> str:
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def _assistant_line(text: str, ts: str = "2026-10-27T09:00:05.000Z") -> str:
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


@pytest.fixture
def lane(tmp_path):
    """真引擎池上的默认世界：左脑 lane 开启时把 stub 嵌入当作标定模型（lane 生效），spool 在 tmp_path 下，
    抽取用假 LLM，钩子由池挂好，默认世界按会话触发打开。不起后台线程。产出 (pool, 引擎)。"""
    cfg = _cfg(tmp_path, source_lane_enabled=True, source_lane_model="stub", source_lane_dim=8)
    pool, _reg = _pool(tmp_path, cfg, clock=_Clock())
    pool.set_auto_snapshot("default", True, 0)
    with pool.lease("default") as eng:
        pass
    # pytest 拆 fixture 时还留着它产出的值：产出一个列表、拆之前清空，close_all 才等得到旧 Runtime 回收。
    box = [pool, eng]
    eng = None
    yield box
    box.clear()
    pool.close_all()


def _spool_job(tmp_path, eng, job_name: str, session_id: str | None):
    """往引擎的 spool 分区放一个 transcript 摄入 job，返回 (job 路径, job dict)。"""
    transcript = tmp_path / f"{job_name}.jsonl"
    transcript.write_text("\n".join([_user_line(f"{job_name}: Bob owns auth"),
                                     _assistant_line("noted")]))
    job = {"transcript_path": str(transcript), "cwd": "/proj/foo", "tenant": eng.tenant}
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
    if eng._lock.acquire(blocking=False):
        eng._lock.release()
        return True
    return False


def test_ingest_snapshots_a_new_session_before_writing_it_and_only_once(lane, tmp_path):
    pool, eng = lane
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    snaps = _auto_snaps(pool)
    assert [(s["kind"], s["reason"], s["session_id"]) for s in snaps] == [("auto", "session", "s-1")]
    assert _session_units(snaps[0]["path"], "s-1") == 0, "快照必须在这个会话写入之前拍"
    assert _session_units(eng._db_path, "s-1") == 2
    assert (eng._ingest_spool / "done" / "job-1.json").exists()
    _spool_job(tmp_path, eng, "job-2", "s-1")                     # 同一会话的第二个 job
    assert eng._ingest_drain_once() == "done"
    assert len(_auto_snaps(pool)) == 1, "同一会话不再拍"
    _spool_job(tmp_path, eng, "job-3", "s-2")                     # 新会话再拍一份
    assert eng._ingest_drain_once() == "done"
    newest = _auto_snaps(pool)[0]
    assert (newest["reason"], newest["session_id"]) == ("session", "s-2")
    assert _session_units(newest["path"], "s-1") == 4 and _session_units(newest["path"], "s-2") == 0
    rows = {row["session_id"]: row for row in pool.list_sessions("default", 200)["sessions"]}
    assert rows["s-1"]["anchor"]["snapshot_id"] == snaps[0]["id"]
    assert rows["s-2"]["anchor"]["snapshot_id"] == newest["id"]


def test_ingest_calls_the_session_hook_outside_the_engine_lock_and_before_any_write(lane, tmp_path):
    _pool_, eng = lane
    calls = []

    def _spy(session_id):
        _worker, free = _call_in_thread(_engine_lock_is_free, eng)
        calls.append((session_id, free.get("value"), _session_units(eng._db_path, session_id),
                      _count(eng._db_path)))
    eng.session_snapshot_hook = _spy
    _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    assert calls == [("s-1", True, 0, 0)]
    # 没有会话 id：不调钩子（这种 job 随后在 retain_source_turns 里失败，走有界重试）。
    for job_name, session_id in (("job-2", ""), ("job-3", None)):
        job_path, _job = _spool_job(tmp_path, eng, job_name, session_id)
        assert eng._ingest_drain_once() == "deferred"
        job_path.unlink()
    assert calls == [("s-1", True, 0, 0)]


def test_ingest_defers_the_job_without_an_attempt_while_the_world_lock_is_held(lane, tmp_path):
    pool, eng = lane
    job_path, job = _spool_job(tmp_path, eng, "job-1", "s-1")
    holder, release = _hold_world_lock(pool)
    try:
        drainer, result = _call_in_thread(eng._ingest_drain_once)
        assert not drainer.is_alive(), "摄入线程在世界锁上等了"
        assert result == {"value": "deferred"}
    finally:
        release.set()
        holder.join(10)
    assert json.loads(job_path.read_text()) == job, "放回 spool 的 job 原样不变（不计 attempts）"
    assert not job_path.with_suffix(".json.processing").exists()
    assert _session_units(eng._db_path, "s-1") == 0 and _count(eng._db_path) == 0
    assert _auto_snaps(pool) == [] and pool.auto_snapshot_status()["failures"] == 0
    assert eng._ingest_drain_once() == "done"                     # 锁放开后照常拍、照常写
    snaps = _auto_snaps(pool)
    assert [s["session_id"] for s in snaps] == ["s-1"]
    assert _session_units(snaps[0]["path"], "s-1") == 0
    assert _session_units(eng._db_path, "s-1") == 2


def test_ingest_is_not_deferred_by_the_world_lock_when_on_session_is_off(lane, tmp_path):
    pool, eng = lane
    pool.set_auto_snapshot("default", False, 24)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    holder, release = _hold_world_lock(pool)
    try:
        drainer, result = _call_in_thread(eng._ingest_drain_once)
        assert not drainer.is_alive(), "摄入线程在世界锁上等了"
        assert result == {"value": "done"}, result
    finally:
        release.set()
        holder.join(10)
    assert (eng._ingest_spool / "done" / "job-1.json").exists()
    assert _session_units(eng._db_path, "s-1") == 2
    assert _auto_snaps(pool) == [] and pool.auto_snapshot_status()["failures"] == 0


def test_ingest_continues_when_the_session_snapshot_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, source_lane_enabled=True, source_lane_model="stub", source_lane_dim=8)
    app = create_app(cfg, engine_factory=_stub_factory)
    pool = app.state.pool
    client = TestClient(app)
    pool.set_auto_snapshot("default", True, 0)
    with pool.lease("default") as eng:
        def _no_space(*_args, **_kwargs):
            raise _core.WorldStoreError("insufficient_disk: need 3x live bytes")
        monkeypatch.setattr(_core, "world_take_auto_snapshot", _no_space)
        _spool_job(tmp_path, eng, "job-1", "s-1")
        assert eng._ingest_drain_once() == "done"
        assert (eng._ingest_spool / "done" / "job-1.json").exists()
        assert _session_units(eng._db_path, "s-1") == 2
    eng = None
    assert _auto_snaps(pool) == []
    body = client.get("/api/ingest_status").json()
    assert body["done"] == 1 and body["failed"] == 0
    assert (body["auto_snapshot_failures"], body["auto_snapshot_last_error"]) == (
        1, "insufficient_disk")
    status = client.get("/api/worlds").json()["auto_snapshot_status"]
    assert (status["failures"], status["last_error"]) == (1, "insufficient_disk")
    assert _SECONDS_ISO.fullmatch(status["last_error_at"])
    monkeypatch.undo()
    pool.close_all()


def test_ingest_discards_the_job_when_memory_is_cleared_during_the_session_snapshot(lane, tmp_path):
    """快照可能要几秒：这期间发生的清空让 job 作废，来源表一行不写。"""
    pool, eng = lane
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
    assert len(_auto_snaps(pool)) == 1


def test_deferred_job_is_discarded_not_requeued_when_memory_is_cleared_during_the_hook(lane,
                                                                                       tmp_path):
    """job claim 之后、钩子推迟之前发生了清空：推迟分支若原样放回，下一轮会把清空前的会话写回库里。
    必须改进 discarded/。"""
    pool, eng = lane
    real_hook = eng.session_snapshot_hook
    calls = []

    def _clear_then_defer(session_id):
        calls.append(session_id)
        if len(calls) == 1:
            eng.clear_memory(confirm=eng.tenant)          # 用户在钩子的窗口里点了清空
            raise SessionSnapshotDeferred("world lock busy")
        real_hook(session_id)
    eng.session_snapshot_hook = _clear_then_defer
    job_path, _job = _spool_job(tmp_path, eng, "job-1", "s-1")
    assert eng._ingest_drain_once() == "done"
    spool = eng._ingest_spool
    assert (spool / "discarded" / "job-1.json").exists()
    assert not job_path.exists() and not job_path.with_suffix(".json.processing").exists()
    assert eng._ingest_drain_once() == "empty"
    assert calls == ["s-1"]
    assert _session_units(eng._db_path, "s-1") == 0 and _count(eng._db_path) == 0
    assert _auto_snaps(pool) == []


def test_clear_memory_cannot_land_between_the_generation_check_and_the_source_write(
        lane, tmp_path, monkeypatch):
    """代数检查与 retain_source_turns 之间若放开 engine._lock，另一个线程的 clear_memory 会落在两者之间，
    清空前的 transcript 在清空之后写回来源表。两者必须同一把锁。必须两个真线程（engine._lock 是 RLock）。"""
    _pool_, eng = lane
    import starling.dashboard.engine as engine_mod
    real_retain = engine_mod._core.retain_source_turns
    in_retain, may_write = threading.Event(), threading.Event()

    def _paused_retain(*args, **kwargs):
        in_retain.set()
        assert may_write.wait(10), "测试没有放行写入"
        return real_retain(*args, **kwargs)
    monkeypatch.setattr(engine_mod._core, "retain_source_turns", _paused_retain)
    _spool_job(tmp_path, eng, "job-1", "s-1")
    drain_result: dict = {}
    clear_result: dict = {}

    def _drain():
        try:
            drain_result["value"] = eng._ingest_drain_once()
        except BaseException as exc:  # noqa: BLE001
            drain_result["error"] = exc

    def _clear():
        try:
            clear_result["value"] = eng.clear_memory(confirm=eng.tenant)
        except BaseException as exc:  # noqa: BLE001
            clear_result["error"] = exc

    drainer = threading.Thread(target=_drain, daemon=True)
    clearer = threading.Thread(target=_clear, daemon=True)
    try:
        drainer.start()
        assert in_retain.wait(10), "drain 没走到 retain_source_turns"
        clearer.start()
        clearer.join(timeout=0.2)
        assert clearer.is_alive(), "retain_source_turns 进行中 clear_memory 不该拿到 engine._lock"
        assert clear_result == {}
    finally:
        may_write.set()
        drainer.join(10)
        clearer.join(10)
    assert not drainer.is_alive() and not clearer.is_alive()
    assert "error" not in clear_result and "error" not in drain_result
    assert _session_units(eng._db_path, "s-1") == 0


def test_ingest_with_the_lane_off_takes_no_session_snapshot(tmp_path):
    """调用点在来源通道的写入里：lane 关闭就没有会话，也就没有按会话的快照（设计如此）。"""
    pool, _reg = _pool(tmp_path, clock=_Clock())
    try:
        pool.set_auto_snapshot("default", True, 0)
        with pool.lease("default") as eng:
            _spool_job(tmp_path, eng, "job-1", "s-1")
            assert eng._ingest_drain_once() == "done"
            assert _count(eng._db_path) >= 1
        eng = None
        assert _auto_snaps(pool) == []
    finally:
        pool.close_all()


def test_ingest_job_without_a_session_id_never_reaches_the_real_hook(lane, tmp_path):
    """空 session_id 交给 C++ 会抛 invalid_auto_snapshot、记成失败；调用点必须先挡掉。"""
    pool, eng = lane
    for job_name, session_id in (("job-1", ""), ("job-2", None)):
        job_path, _job = _spool_job(tmp_path, eng, job_name, session_id)
        eng._ingest_drain_once()
        job_path.unlink(missing_ok=True)
    assert pool.auto_snapshot_status() == {"failures": 0, "last_error": None, "last_error_at": None}
    assert _auto_snaps(pool) == []


def test_ingest_partitions_keep_worlds_apart(tmp_path):
    """每个世界只消费自己的摄入分区：放进 w1 分区的 job 只写进 w1，默认世界不碰它。"""
    cfg = _cfg(tmp_path, source_lane_enabled=True, source_lane_model="stub", source_lane_dim=8)
    pool, reg = _pool(tmp_path, cfg, clock=_Clock())
    try:
        pool.create_world("w1", "")
        with pool.lease("default") as dflt, pool.lease("w1") as w1:
            assert dflt._ingest_spool != w1._ingest_spool
            _spool_job(tmp_path, w1, "job-1", "s-1")
            assert dflt._ingest_drain_once() == "empty"
            assert w1._ingest_drain_once() == "done"
        dflt = w1 = None
        assert _session_units(_db(reg, "w1"), "s-1") == 2
        assert _session_units(_db(reg, "default"), "s-1") == 0
    finally:
        pool.close_all()


def test_ingest_status_without_a_registry_reports_zero_auto_snapshot_failures():
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    with TestClient(app) as client:
        body = client.get("/api/ingest_status").json()
    assert (body["auto_snapshot_failures"], body["auto_snapshot_last_error"]) == (0, None)


# ───────────────────────── lifespan 接线 ─────────────────────────

@pytest.mark.parametrize("shape", ["injected", "built_by_lifespan"])
def test_lifespan_attaches_the_hook_before_ingest_and_runs_the_scheduler(tmp_path, monkeypatch,
                                                                          shape):
    cfg = _cfg(tmp_path, tick_interval_s=60.0 if shape == "built_by_lifespan" else 0)
    hooks_at_start = []
    real_start = DashboardEngine.start_ingest_worker

    def _spy_start(self, *args, **kwargs):
        hooks_at_start.append(self.session_snapshot_hook)
        return real_start(self, *args, **kwargs)
    monkeypatch.setattr(DashboardEngine, "start_ingest_worker", _spy_start)
    app = create_app(cfg, engine=DashboardEngine(cfg) if shape == "injected" else None,
                     engine_factory=_stub_factory)
    pool = app.state.pool
    before = set(_scheduler_threads())
    with TestClient(app):
        started = [t for t in _scheduler_threads() if t not in before]
        assert len(started) == 1, "调度线程在跑"
        assert len(hooks_at_start) == 1 and hooks_at_start[0] is not None, \
            "摄入线程起来之前钩子就要挂上"
        with pool.lease("default", build=False) as engine:
            assert engine is not None and engine.session_snapshot_hook is not None
        engine = None
    assert all(not t.is_alive() for t in started), "退出 lifespan 之后调度线程已停"


def test_lifespan_without_a_registry_starts_no_scheduler():
    app = create_app(DashboardConfig(db_path=":memory:", token="", tick_interval_s=0))
    before = set(_scheduler_threads())
    with TestClient(app):
        assert [t for t in _scheduler_threads() if t not in before] == []


def test_worlds_opened_after_startup_start_their_own_workers(tmp_path):
    """lifespan 开着后台：之后第一次打开的世界按自己的设置起线程，关停时一并停掉。"""
    app = create_app(_cfg(tmp_path, tick_interval_s=60.0), engine_factory=_stub_factory)
    pool = app.state.pool
    with TestClient(app) as client:
        client.post("/api/worlds", json={"name": "w1", "settings": {
            "background": {"tick_interval_s": 0, "ingest": True}}})
        assert client.post("/api/w/w1/tick", json={}).status_code == 200
        with pool.lease("w1", build=False) as eng:
            threads = (eng._tick_thread, eng._ingest_thread)
        eng = None
        assert threads[0] is None                         # w1 的 tick 关着
        assert threads[1] is not None and threads[1].is_alive()
    assert not threads[1].is_alive()


# ───────────────────────── create_app 与注入的引擎 ─────────────────────────

def test_create_app_warns_when_the_registry_overrides_config(tmp_path, caplog):
    create_app(_cfg(tmp_path)).state.pool.close_all()     # 建注册表：default -> dashboard.db
    caplog.clear()
    with caplog.at_level("WARNING"):
        create_app(_cfg(tmp_path)).state.pool.close_all()
    assert "overrides config" not in caplog.text          # 一致时不吵
    with caplog.at_level("WARNING"):
        app = create_app(_cfg(tmp_path, db_path=str(tmp_path / "other.db")))
    assert "overrides config" in caplog.text              # 例如 STARLING_DASH_DB 指到别处
    assert app.state.pool.context("default").db_path == str(tmp_path / "dashboard.db")
    app.state.pool.close_all()


def test_create_app_rejects_engine_for_another_db(tmp_path):
    eng = DashboardEngine(_cfg(tmp_path, db_path=str(tmp_path / "other.db")))
    with pytest.raises(ValueError) as ei:
        create_app(_cfg(tmp_path), engine=eng)
    assert "other.db" in str(ei.value) and "dashboard.db" in str(ei.value)
    eng.close()


def test_create_app_rejects_engine_when_the_registry_overrides_config(tmp_path):
    """注册表早已建好、记的默认世界库与 config 不同：按 config 建好的引擎打开的是另一个库，必须拒绝（比
    引擎实际打开的库，不比 config 字段）。"""
    create_app(_cfg(tmp_path)).state.pool.close_all()     # 注册表：default -> dashboard.db
    cfg = _cfg(tmp_path, db_path=str(tmp_path / "elsewhere.db"))
    eng = DashboardEngine(cfg)
    with pytest.raises(ValueError):
        create_app(cfg, engine=eng)
    eng.close()


def test_create_app_accepts_a_matching_injected_engine(tmp_path):
    cfg = _cfg(tmp_path)
    eng = DashboardEngine(cfg)
    app = create_app(cfg, engine=eng)
    with app.state.pool.lease("default", build=False) as seeded:
        assert seeded is eng
    seeded = eng = None
    app.state.pool.close_all()


# ───────────────────────── 引擎的 quiesce 与线程句柄 ─────────────────────────

class _StuckThread:
    """join 不动、一直 is_alive 的线程替身。"""
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def test_quiesce_stops_threads(tmp_path):
    eng = DashboardEngine(_cfg(tmp_path))
    eng.start_background_tick(60.0)
    eng.start_ingest_worker(poll_interval_s=60.0)
    assert eng.quiesce() is True
    assert eng._tick_thread is None and eng._ingest_thread is None
    eng.close()


def test_quiesce_failure_keeps_stuck_worker_runnable(tmp_path):
    """join 超时：返回 False，句柄原样保留，且撤销停止信号。否则调用方恢复线程时 start_* 直接 no-op，这条
    线程一旦退出就再也没人重启；句柄和事件不一致还会让之后的 stop_ingest_worker() 在 None.set() 上炸。"""
    eng = DashboardEngine(_cfg(tmp_path))
    stuck, stop = _StuckThread(), threading.Event()
    eng._ingest_thread, eng._ingest_stop = stuck, stop
    assert eng.quiesce() is False
    assert eng._ingest_thread is stuck and eng._ingest_stop is stop
    assert not stop.is_set() and eng.quiesce_stuck == ("ingest",)
    eng._ingest_thread = eng._ingest_stop = None     # 收尾，别留下假线程
    eng.close()


def test_quiesce_timeout_is_a_total_budget_for_both_threads(tmp_path):
    """两个线程都卡住时，总耗时受 timeout_s 约束（两次 join 各给满的话最坏是 2×timeout_s）。"""
    eng = DashboardEngine(_cfg(tmp_path))
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
        assert elapsed < 1.5, f"quiesce took {elapsed:.2f}s for a 1.0s total budget"
        assert set(eng.quiesce_stuck) == {"ingest", "tick"}
    finally:
        stuck.set()
        eng._ingest_thread.join(5)
        eng._tick_thread.join(5)
        eng._ingest_thread = eng._ingest_stop = eng._tick_thread = eng._tick_stop = None
        eng.close()


def _block_first_start_of(monkeypatch, name: str):
    """把名为 name 的线程的第一次 Thread.start() 卡住，直到返回的 release 被 set；卡住时 set in_start。"""
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
def test_worker_handle_is_set_only_after_the_thread_has_started(tmp_path, monkeypatch, blocked):
    """start_background_tick / start_ingest_worker 在 Thread.start() 返回之后才挂句柄：卡在 start() 里的这段
    时间，quiesce 看不到句柄，不会去 join 一个没启动的线程（那会抛 RuntimeError，并留下句柄还在、线程已退出
    的引擎，此后 start_* 一律 no-op）。"""
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
        thread = eng._tick_thread if blocked == "starling-bg-tick" else eng._ingest_thread
        assert thread is not None and thread.is_alive() and thread.name == blocked
        del thread
    finally:
        release.set()
        starter.join(15)
        eng.quiesce()
        eng.close()


def test_lease_after_a_failed_worker_start_keeps_serving_and_retries(tmp_path):
    """后台开着时起线程失败（例如摄入 spool 读不出）：租约照常拿到同一个引擎，世界标记 workers-lost；下一次
    租约补起线程（裁定 R23）。"""
    pool, _reg = _pool(tmp_path)
    try:
        pool.start_background(stats_interval_s=3600.0, scheduler_poll_s=3600.0,
                              scheduler_first_delay_s=3600.0)
        pool.create_world("w1", "")
        broken = {"on": True}
        real_start = DashboardEngine.start_ingest_worker

        def _maybe_broken(self, *args, **kwargs):
            if broken["on"]:
                raise RuntimeError("spool unreadable")
            return real_start(self, *args, **kwargs)
        DashboardEngine.start_ingest_worker = _maybe_broken
        try:
            with pool.lease("w1") as first:
                first_id = id(first)
                assert first._ingest_thread is None
            first = None
            assert pool.workers_lost_worlds() == ["w1"]
            broken["on"] = False
            with pool.lease("w1") as again:
                assert id(again) == first_id
                assert again._ingest_thread is not None and again._ingest_thread.is_alive()
            again = None
            assert pool.workers_lost_worlds() == []
        finally:
            DashboardEngine.start_ingest_worker = real_start
    finally:
        pool.close_all()
