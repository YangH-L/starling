"""看板按世界类型只读打开、分叉与升级种子的 HTTP 路由（记忆服务第 3 期 Task 3.3，spec §6.1–§6.4）。

打开方式由 C++ 判（_core.world_decide_open_mode）：只读世界（含种子）与可回收世界看板一律只读打开，不跑迁移、
不起后台线程、写入类请求回 409 world_read_only；其余世界照旧可写。列表与详情带注册表的原始标记（seed、
read_only、reclaimable 等）与看板实际执行的 open_mode（裁定 R18）；schema_behind 列表只给开着的世界、详情对
关着的世界短暂只读打开来算（裁定 R17）。
"""
import hashlib
import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from starling import _core
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine

_STUB = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
         '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
         '"polarity":"POS","nesting_depth":0}]')


def _fake():
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    return fake


def _stub_factory(ctx, providers):
    """池的引擎工厂：照常建引擎（只读与否由 ctx 决定），再换上离线的抽取适配器。"""
    engine = DashboardEngine(ctx, providers)
    engine.llm = _fake()
    return engine


@pytest.fixture
def client(tmp_path):
    # 进 lifespan：后台开着（tick 600 秒、摄入按世界设置），只读世界必须不起线程。
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    with TestClient(create_app(cfg, engine_factory=_stub_factory)) as test_client:
        yield test_client


def _pool(client):
    return client.app.state.pool


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _dir_files(db_path: str) -> set[str]:
    return set(os.listdir(os.path.dirname(db_path)))


def _digest(db_path: str, tenant: str) -> str:
    adapter = _core.SqliteAdapter.open_read_only(db_path)
    try:
        return _core.memory_digest(adapter.connection(), tenant)
    finally:
        adapter.close()


def _source_with_content(client, name: str = "src") -> dict:
    """建世界 name，经 HTTP 写一条、跑一轮 tick（占位嵌入把向量补上，召回才有东西可读）。"""
    rec = _pool(client).create_world(name, "")
    resp = client.post(f"/api/w/{name}/remember", json={"text": "Bob owns auth"})
    assert resp.status_code == 200, resp.text
    assert client.post(f"/api/w/{name}/tick", json={}).status_code == 200
    return rec


def _fork(client, source: str, new_name: str, **body) -> dict:
    resp = client.post(f"/api/worlds/{source}/fork", json={"new_name": new_name, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(client) -> dict:
    return {row["name"]: row for row in client.get("/api/worlds").json()["worlds"]}


def _engine_of(pool, world: str):
    with pool.lease(world, build=False) as engine:
        return engine


def _lock_is_free(lock) -> bool:
    """另一个线程能不能拿到 lock（RLock 对持有它的线程自己总是可重入）。"""
    got: list = []

    def _try():
        ok = lock.acquire(blocking=False)
        if ok:
            lock.release()
        got.append(ok)
    worker = threading.Thread(target=_try)
    worker.start()
    worker.join()
    return got[0]


# ───────────────────────── 分叉路由 ─────────────────────────

def test_fork_through_http_digest_matches(client, monkeypatch):
    pool = _pool(client)
    src = _source_with_content(client)
    assert "src" in pool.open_worlds()

    # 裁定 R5：开着（可写）的来源走引擎自己的连接，持着引擎锁；关着的来源持它的世界锁走 world_fork；
    # 快照为来源直接 world_fork。用替身记下走的是哪条路、当时锁的状态。
    calls: list = []
    real_from_conn, real_fork = _core.world_fork_from_connection, _core.world_fork

    def _spy_from_conn(reg, conn, source, new_name, opts):
        engine = _engine_of(pool, source)
        calls.append(("from_connection", source, _lock_is_free(engine._lock)))
        engine = None
        return real_from_conn(reg, conn, source, new_name, opts)

    def _spy_fork(reg, source, new_name, opts):
        calls.append(("fork", source, opts.get("snapshot_id"),
                      _lock_is_free(pool._world_lock(source))))
        return real_fork(reg, source, new_name, opts)
    monkeypatch.setattr(_core, "world_fork_from_connection", _spy_from_conn)
    monkeypatch.setattr(_core, "world_fork", _spy_fork)

    live = _digest(src["db_path"], src["tenant"])
    out = _fork(client, "src", "rollout-1", labels={"run": "r1"}, retention_group="grp-a",
                reclaimable=True)
    assert out["digest"] == live
    row = out["world"]
    assert row["name"] == "rollout-1" and row["fork_of_world"] == "src"
    assert row["fork_of_snapshot"] is None and row["labels"] == {"run": "r1"}
    assert row["retention_group"] == "grp-a" and row["reclaimable"] is True
    assert row["tenant"] == "src" and "db_path" not in row and "snapshot_dir" not in row
    assert calls[-1] == ("from_connection", "src", False)          # 引擎锁被持着
    forked = pool.record("rollout-1")
    assert _digest(forked["db_path"], forked["tenant"]) == live
    # 裁定 R22 M6：新分叉的世界没打开过，列表用注册表的统计缓存；C++ 登记时已从复制出来的库算好。
    assert "rollout-1" not in pool.open_worlds()
    assert _rows(client)["rollout-1"]["statements"] == 1 == forked["statements"]

    # 关着的来源：世界锁持着，不经引擎。
    pool.close_world("src")
    out = _fork(client, "src", "rollout-2")
    assert out["digest"] == live and calls[-1] == ("fork", "src", None, False)
    assert "src" not in pool.open_worlds()                          # 分叉不打开来源

    # 快照为来源：fork_of_snapshot 记下快照 id。
    snap = client.post("/api/worlds/src/snapshots", json={}).json()
    out = _fork(client, "src", "replay-1", snapshot_id=snap["id"])
    assert out["world"]["fork_of_snapshot"] == snap["id"] and out["digest"] == live
    assert calls[-1][:3] == ("fork", "src", snap["id"])

    # 设置覆盖经 C++ 合并（与建世界同一条路）。
    out = _fork(client, "src", "tuned-1", settings={"source_lane": {"k_source": 9}})
    detail = client.get("/api/worlds/tuned-1").json()
    assert detail["settings"]["source_lane"]["k_source"] == 9

    # 错误码：重名 409、来源不存在 404、名字不合规则 400、快照不存在 404、设置带服务端键 400。
    for body, source, expected in (
            ({"new_name": "rollout-1"}, "src", (409, "world_exists")),
            ({"new_name": "fresh"}, "nope", (404, "world_not_found")),
            ({"new_name": "../x"}, "src", (400, "invalid_world_name")),
            ({"new_name": "fresh", "snapshot_id": "20261106T000000Z-0000"}, "src",
             (404, "snapshot_not_found")),
            ({"new_name": "fresh", "settings": {"embedding": {"key_set": True}}}, "src",
             (400, "invalid_settings"))):
        resp = client.post(f"/api/worlds/{source}/fork", json=body)
        assert (resp.status_code, resp.json()["detail"]) == expected, body
    assert "fresh" not in _rows(client)



def test_fork_of_default_world_with_foreign_tenant_rows_is_409(client):
    """裁定 R29（审查 I2）：默认世界（原地的 dashboard.db）可以按租户过滤地存多个租户，分叉复制整个文件，新世界
    每次打开都会 world_tenant_mismatch。C++ 分叉在 partial 上核对租户，不过就 409 world_tenant_mismatch，什么都不
    登记、不留目录。"""
    pool = _pool(client)
    resp = client.post("/api/remember", json={"text": "Bob owns auth"})
    assert resp.status_code == 200, resp.text
    db = pool.record("default")["db_path"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
            "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
            "VALUES('x-1','intruder','hx','conversation','default','inline','normal',"
            "'audit_retain','2026-10-25T00:00:00Z','x-1')")
    conn.close()
    worlds_root = os.path.join(os.path.dirname(db), "worlds")
    dirs = set(os.listdir(worlds_root)) if os.path.isdir(worlds_root) else set()
    resp = client.post("/api/worlds/default/fork", json={"new_name": "rollout-1"})
    assert (resp.status_code, resp.json()["detail"]) == (409, "world_tenant_mismatch")
    assert "rollout-1" not in _rows(client)
    assert (set(os.listdir(worlds_root)) if os.path.isdir(worlds_root) else set()) == dirs


def test_fork_of_a_zvec_source_is_409_unsupported_backend(client):
    """裁定 R31 M4：分叉把来源世界的向量后端（与快照路由同一取法）交给 C++，非 sqlite 回 409
    snapshot_unsupported_backend（与快照同一规则），什么都不登记。"""
    pool = _pool(client)
    assert client.post("/api/remember", json={"text": "Bob owns auth"}).status_code == 200
    pool._config.vector_backend = "zvec"
    try:
        resp = client.post("/api/worlds/default/fork", json={"new_name": "rollout-1"})
        assert (resp.status_code, resp.json()["detail"]) == (409, "snapshot_unsupported_backend")
        assert client.post("/api/worlds/default/snapshots", json={}).json()["detail"] == \
            "snapshot_unsupported_backend"
    finally:
        pool._config.vector_backend = "sqlite"
    assert "rollout-1" not in _rows(client)
    assert _fork(client, "default", "rollout-1")["world"]["name"] == "rollout-1"


def test_fork_of_schema_behind_source_is_409(client):
    pool = _pool(client)
    src = _source_with_content(client)
    pool.close_world("src")
    with sqlite3.connect(src["db_path"]) as conn:
        conn.execute("DELETE FROM schema_migrations "
                     "WHERE version = (SELECT MAX(version) FROM schema_migrations)")
    conn.close()
    resp = client.post("/api/worlds/src/fork", json={"new_name": "rollout-1"})
    assert (resp.status_code, resp.json()["detail"]) == (409, "world_schema_behind")
    assert "rollout-1" not in _rows(client)


# ───────────────────────── 只读打开 ─────────────────────────

def test_reclaimable_world_opens_read_only_and_bytes_unchanged(client):
    pool = _pool(client)
    _source_with_content(client)
    _fork(client, "src", "rollout-1", reclaimable=True)
    rec = pool.record("rollout-1")
    assert rec["settings"]["background"]["ingest"]                 # 存着的设置（继承自来源）后台开着
    # 裁定 R33（R22 M4）：详情给的是生效的设置。可回收世界看板只读打开，后台全关（判定在 C++），列表行的
    # auto_snapshot 同样全关。
    detail = client.get("/api/worlds/rollout-1").json()
    assert detail["open_mode"] == "ro"
    assert detail["settings"]["background"] == {
        "tick_interval_s": 0.0, "ingest": False,
        "auto_snapshot": {"on_session": False, "interval_hours": 0}}
    assert detail["auto_snapshot"] == {"on_session": False, "interval_hours": 0}
    assert client.get("/api/worlds/src").json()["settings"]["background"]["ingest"] is True
    db = rec["db_path"]
    sha, files = _sha256(db), _dir_files(db)

    rows = client.get("/api/w/rollout-1/statements").json()["rows"]
    assert [row["object_value"] for row in rows] == ["auth"]
    resp = client.post("/api/w/rollout-1/recall", json={"query": "who owns auth"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"], "召回要真的读到分叉过来的语句"
    assert client.get("/api/w/rollout-1/working_set",
                      params={"interlocutor": "claude"}).status_code == 200

    assert "rollout-1" in pool.open_worlds()
    engine = _engine_of(pool, "rollout-1")
    assert engine.read_only is True and engine._rt.adapter.read_only is True
    assert engine._tick_thread is None and engine._ingest_thread is None
    assert engine.session_snapshot_hook is None
    engine = None

    pool.close_world("rollout-1")
    assert _sha256(db) == sha
    after = _dir_files(db)
    assert after == files, after - files                             # 没有新的 -wal / -shm / -journal
    assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in after)

    # 可写的普通世界照旧可写打开、起后台线程（对照）。
    engine = _engine_of(pool, "src")
    assert engine.read_only is False and engine._tick_thread is not None
    engine = None


def _wal_header(db_path: str) -> bool:
    """库头第 19 字节（读版本）是 2：WAL 模式的库。"""
    with open(db_path, "rb") as fh:
        return fh.read(20)[19] == 2


def _inspect_leaves_no_trace(client, name: str) -> tuple[list, list]:
    """详情、语句检视、召回各走一遍（召回会只读打开引擎），每一步之后目录文件集合不变；关闭之后 sha256 不变。
    返回 (语句行, 召回结果)。"""
    pool = _pool(client)
    db = pool.record(name)["db_path"]
    assert _wal_header(db), "这条测试要的是 WAL 头、旁边没有 -wal/-shm 的库"
    sha, files = _sha256(db), _dir_files(db)
    assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in files), files
    detail = client.get(f"/api/worlds/{name}")
    assert detail.status_code == 200, detail.text
    assert "stats_error" not in detail.json()
    assert _dir_files(db) == files, ("detail", _dir_files(db) - files)
    resp = client.get(f"/api/w/{name}/statements")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert _dir_files(db) == files, ("statements", _dir_files(db) - files)
    resp = client.post(f"/api/w/{name}/recall", json={"query": "who owns auth"})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert _dir_files(db) == files, ("recall", _dir_files(db) - files)
    assert name in pool.open_worlds()
    pool.close_world(name)
    assert _dir_files(db) == files, ("close", _dir_files(db) - files)
    assert _sha256(db) == sha
    return rows, results


def test_upgrade_seed_output_wal_header_inspect_leaves_no_sidecars(client):
    """裁定 R28（审查 I1）：upgrade_seed 的产物在副本上跑过迁移、经写连接关闭，库头是 WAL、旁边没有 -wal/-shm。
    看板的每条只读打开（C++ world_stats / 只读引擎，Python queries.open_ro）都走 C++ 的同一条规则，不在旁边建文件。"""
    _source_with_content(client)
    _fork(client, "src", "seed-1", seed=True)
    resp = client.post("/api/worlds/seed-1/upgrade_seed", json={"new_name": "seed-2"})
    assert resp.status_code == 200, resp.text
    rows, results = _inspect_leaves_no_trace(client, "seed-2")
    assert [row["object_value"] for row in rows] == ["auth"]
    assert results, "召回要真的读到种子里的语句"


def test_empty_seed_from_registry_create_inspect_leaves_no_sidecars(client):
    """裁定 R28、R34 M8：_core.WorldRegistry.create(seed=True) 建的空种子（create_world 初始化过库：WAL 头、旁边
    没有 -wal/-shm）。同样的检视不在旁边建文件、不改字节。"""
    pool = _pool(client)
    pool.registry.create("seed-e", "", {}, None, "2026-11-06T00:00:00Z", seed=True)
    rows, results = _inspect_leaves_no_trace(client, "seed-e")
    assert rows == [] and results == []


@pytest.mark.parametrize("kind", ["reclaimable", "seed"])
def test_write_to_read_only_world_is_409_world_read_only(client, kind):
    _source_with_content(client)
    _fork(client, "src", "ro-1", **{kind: True})
    db = _pool(client).record("ro-1")["db_path"]
    sha = _sha256(db)
    stmt_id = client.get("/api/w/ro-1/statements").json()["rows"][0]["id"]
    writes = (("/remember", {"text": "Carol owns billing"}),
              ("/converse", {"message": "who owns billing?"}),
              ("/tick", {}),
              ("/forget", {"ids": [stmt_id]}),
              ("/clear_memory", {"confirm": "src"}),
              ("/review", {"stmt_id": stmt_id}),
              ("/replay_trigger", {"mode": "idle"}),
              ("/reconsolidate", {"stmt_id": stmt_id}),
              ("/commitment/fulfill", {"stmt_id": stmt_id}),
              ("/commitment/withdraw", {"stmt_id": stmt_id}))
    for path, body in writes:
        resp = client.post(f"/api/w/ro-1{path}", json=body)
        assert (resp.status_code, resp.json()) == (409, {"detail": "world_read_only"}), path
    with client.websocket_connect("/ws/converse") as ws:
        ws.send_json({"message": "who owns billing?", "world": "ro-1"})
        assert ws.receive_json() == {"type": "error", "error": "world_read_only"}
    assert client.get("/api/w/ro-1/statements").json()["rows"][0]["id"] == stmt_id
    _pool(client).close_world("ro-1")
    assert _sha256(db) == sha


def test_read_only_engine_maps_a_real_sqlite_readonly_write_to_409(client, monkeypatch):
    """裁定 R33（替代 R22 M3）：真实只读引擎上一次真的走到 SQLite 写的调用。core.tick 的 common_ground 降级扫描在
    只读连接上执行 UPDATE，C++ 抛带类型的 _core.ReadOnlyWriteRejected；引擎按类型（不按错误文本）报 world_read_only。
    这里让 /recall 的读路径调到它（引擎外的写入口本来就被 _require_writable 挡在前面）。"""
    _source_with_content(client)
    _fork(client, "src", "rollout-1", reclaimable=True)
    assert client.post("/api/w/rollout-1/recall", json={"query": "auth"}).status_code == 200
    engine = _engine_of(_pool(client), "rollout-1")
    core = engine._core
    engine = None
    with pytest.raises(_core.ReadOnlyWriteRejected) as ei:          # 真的是 C++ 抛的、带类型
        core.tick("2026-11-06T00:00:00Z")
    assert isinstance(ei.value, RuntimeError)
    del ei
    monkeypatch.setattr(core, "recall", lambda *_a, **_k: core.tick("2026-11-06T00:00:00Z"))
    resp = client.post("/api/w/rollout-1/recall", json={"query": "auth"})
    assert (resp.status_code, resp.json()) == (409, {"detail": "world_read_only"})


def test_read_only_engine_maps_write_rejections_to_world_read_only(client, monkeypatch):
    """读路径上万一碰到写入拒绝（C++ 写门的 WriteGateRejected，或只读连接拒绝写入的 ReadOnlyWriteRejected），只读引擎
    一律报 world_read_only（409），不报 500。文本像只读拒绝的普通 RuntimeError 不算（按类型认，裁定 R33）。"""
    _source_with_content(client)
    _fork(client, "src", "rollout-1", reclaimable=True)
    assert client.post("/api/w/rollout-1/recall", json={"query": "auth"}).status_code == 200
    engine = _engine_of(_pool(client), "rollout-1")
    core = engine._core
    engine = None
    raised = {"exc": None}

    def _recall(*_args, **_kwargs):
        raise raised["exc"]
    monkeypatch.setattr(core, "recall", _recall)
    for exc in (_core.WriteGateRejected("write rejected: runtime not accepting writes"),
                _core.ReadOnlyWriteRejected("Stmt: step failed: attempt to write a readonly database")):
        raised["exc"] = exc
        resp = client.post("/api/w/rollout-1/recall", json={"query": "auth"})
        assert (resp.status_code, resp.json()) == (409, {"detail": "world_read_only"}), exc
    for exc in (ValueError("something else"),
                RuntimeError("Stmt: step failed: attempt to write a readonly database")):
        raised["exc"] = exc
        with pytest.raises(type(exc)):
            client.post("/api/w/rollout-1/recall", json={"query": "auth"})



def test_restore_of_a_seed_is_409_world_read_only(client):
    """裁定 R31 M1：种子（只读）不能恢复。C++ 预检就拒绝（world_read_only → 409），库字节不变、不建归档。"""
    pool = _pool(client)
    _source_with_content(client)
    _fork(client, "src", "seed-1", seed=True)
    snap = client.post("/api/worlds/seed-1/snapshots", json={})
    assert snap.status_code == 200, snap.text
    db = pool.record("seed-1")["db_path"]
    sha, files = _sha256(db), _dir_files(db)
    resp = client.post("/api/worlds/seed-1/rollback",
                       json={"snapshot_id": snap.json()["id"], "confirm": "seed-1"})
    assert (resp.status_code, resp.json()["detail"]) == (409, "world_read_only")
    assert _sha256(db) == sha and _dir_files(db) == files
    assert pool.archives("seed-1") == []

# ───────────────────────── 列表、详情与升级种子 ─────────────────────────

def test_seed_shows_flags_in_list(client):
    pool = _pool(client)
    _source_with_content(client)
    snap = client.post("/api/worlds/src/snapshots", json={}).json()
    _fork(client, "src", "seed-1", seed=True)
    _fork(client, "src", "rollout-1", reclaimable=True, retention_group="grp-a",
          snapshot_id=snap["id"])
    pool.create_world("plain", "")

    upgraded = client.post("/api/worlds/seed-1/upgrade_seed", json={"new_name": "seed-2"})
    assert upgraded.status_code == 200, upgraded.text
    body = upgraded.json()
    assert body["world"]["fork_of_world"] == "seed-1" and body["world"]["seed"] is True
    assert body["digest"] == _digest(pool.record("seed-2")["db_path"], "src")
    resp = client.post("/api/worlds/plain/upgrade_seed", json={"new_name": "seed-3"})
    assert (resp.status_code, resp.json()["detail"]) == (409, "world_not_seed")   # 裁定 R31 M2
    resp = client.post("/api/worlds/seed-1/upgrade_seed", json={"new_name": "seed-2"})
    assert (resp.status_code, resp.json()["detail"]) == (409, "world_exists")

    rows = _rows(client)
    expected = {   # (seed, read_only, reclaimable, open_mode)：read_only 是注册表原始标记（裁定 R18）
        "seed-1": (True, True, False, "ro"), "seed-2": (True, True, False, "ro"),
        "rollout-1": (False, False, True, "ro"),
        "plain": (False, False, False, "rw"), "src": (False, False, False, "rw"),
        "default": (False, False, False, "rw")}
    for name, flags in expected.items():
        row = rows[name]
        assert (row["seed"], row["read_only"], row["reclaimable"], row["open_mode"]) == flags, name
        detail = client.get(f"/api/worlds/{name}").json()
        assert (detail["seed"], detail["read_only"], detail["reclaimable"],
                detail["open_mode"]) == flags, name
    assert rows["seed-1"]["fork_of_world"] == "src" and rows["seed-1"]["fork_of_snapshot"] is None
    assert rows["rollout-1"]["fork_of_snapshot"] == snap["id"]
    assert rows["rollout-1"]["retention_group"] == "grp-a"
    assert rows["plain"]["fork_of_world"] is None and rows["plain"]["retention_group"] is None
    # 种子打开也是只读，不起线程。
    assert client.post("/api/w/seed-1/recall", json={"query": "auth"}).status_code == 200
    engine = _engine_of(pool, "seed-1")
    assert engine.read_only and engine._tick_thread is None and engine._ingest_thread is None
    engine = None


def test_schema_behind_world_opens_and_is_flagged(client, monkeypatch):
    pool = _pool(client)
    _source_with_content(client)
    _fork(client, "src", "old-1", reclaimable=True)
    db = pool.record("old-1")["db_path"]
    with sqlite3.connect(db) as conn:   # 模拟旧版 Starling 写出的库：少了最后一条迁移
        conn.execute("DELETE FROM schema_migrations "
                     "WHERE version = (SELECT MAX(version) FROM schema_migrations)")
    conn.close()
    sha = _sha256(db)

    # 关着：列表不打开库（裁定 R17），给 null；详情短暂只读打开来算。
    opened: list = []
    real_open = _core.SqliteAdapter.open_read_only

    def _spy(path):
        opened.append(path)
        return real_open(path)
    monkeypatch.setattr(_core.SqliteAdapter, "open_read_only", staticmethod(_spy))
    assert _rows(client)["old-1"]["schema_behind"] is None and opened == []
    assert client.get("/api/worlds/old-1").json()["schema_behind"] is True
    assert opened == [db] and "old-1" not in pool.open_worlds()
    monkeypatch.setattr(_core.SqliteAdapter, "open_read_only", staticmethod(real_open))
    assert client.get("/api/worlds/src").json()["schema_behind"] is False

    # 打开：照常打开（不跑迁移），召回可用；列表与详情都标出 schema_behind。
    resp = client.post("/api/w/old-1/recall", json={"query": "who owns auth"})
    assert resp.status_code == 200, resp.text
    assert "old-1" in pool.open_worlds()
    assert _rows(client)["old-1"]["schema_behind"] is True
    assert client.get("/api/worlds/old-1").json()["schema_behind"] is True
    assert _rows(client)["src"]["schema_behind"] is False
    pool.close_world("old-1")
    assert _sha256(db) == sha
