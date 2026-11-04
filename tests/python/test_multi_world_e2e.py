"""多世界端到端（记忆服务第 1 期 Task 8）：全部经 HTTP（TestClient + lifespan），离线（抽取用 stub）。

三条：世界互不可见；回滚一个世界时另一个世界照常写；旧 worlds.json 导入后重启复用 registry.db。
"""
import gc
import json
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from starling import _core, make_stub_llm
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine

_JOIN_S = 60.0


def _stub_factory(ctx, providers):
    """池的引擎工厂：照常建引擎，再换上离线抽取。subject 取世界名，两个世界的数据可以从内容上区分。"""
    engine = DashboardEngine(ctx, providers)
    stub = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"%s",'
            '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
            '"polarity":"POS","nesting_depth":0}]' % ctx.name)
    engine.llm = make_stub_llm(default_response=stub)
    return engine


def _config(data_dir: Path) -> DashboardConfig:
    return DashboardConfig(db_path=str(data_dir / "dashboard.db"), token="",
                           tick_interval_s=600.0, source_lane_enabled=False,
                           ingest_spool_path=str(data_dir / "spool"))


def _app(data_dir: Path, factory=_stub_factory):
    return TestClient(create_app(_config(data_dir), engine_factory=factory))


def _create(client, name: str) -> None:
    resp = client.post("/api/worlds", json={"name": name})
    assert resp.status_code in (200, 201), resp.text


def _remember(client, prefix: str, text: str) -> None:
    resp = client.post(f"{prefix}/remember", json={"text": text})
    assert resp.status_code == 200, resp.text


def _statements(client, prefix: str) -> list[dict]:
    resp = client.get(f"{prefix}/statements")
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


def _count(client, prefix: str) -> int:
    resp = client.get(f"{prefix}/overview")
    assert resp.status_code == 200, resp.text
    return resp.json()["counts"]["statements"]


def _world_names(client) -> set[str]:
    resp = client.get("/api/worlds")
    assert resp.status_code == 200, resp.text
    return {row["name"] for row in resp.json()["worlds"]}


def _subjects(client, prefix: str) -> set[str]:
    return {row["subject_id"] for row in _statements(client, prefix)}


def test_three_worlds_isolated_through_http(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with _app(data) as client:
        _create(client, "w1")
        _create(client, "w2")
        assert {"default", "w1", "w2"} <= _world_names(client)

        # 默认世界：旧路径与显式路径各写一条；w1 写 2 条；w2 写 3 条。
        _remember(client, "/api", "d legacy")
        _remember(client, "/api/w/default", "d explicit")
        for i in range(2):
            _remember(client, "/api/w/w1", f"w1 note {i}")
        for i in range(3):
            _remember(client, "/api/w/w2", f"w2 note {i}")

        counts = {name: _count(client, f"/api/w/{name}") for name in ("default", "w1", "w2")}
        assert counts == {"default": 2, "w1": 2, "w2": 3}
        assert _count(client, "/api") == 2                       # 旧路径 = 默认世界
        for name in ("default", "w1", "w2"):
            subjects = _subjects(client, f"/api/w/{name}")
            assert subjects == {name}, (name, subjects)


def test_rollback_one_world_while_another_writes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    entered = threading.Event()      # w2 的一次 remember 已进入引擎
    release = threading.Event()      # 放行被扣住的那次 remember
    held_once = threading.Event()

    def holding_factory(ctx, providers):
        engine = _stub_factory(ctx, providers)
        if ctx.name == "w2":
            original = engine.remember

            def remember(*args, **kwargs):
                if not held_once.is_set():          # 只扣第一次
                    held_once.set()
                    entered.set()
                    assert release.wait(_JOIN_S), "w2 request never released"
                return original(*args, **kwargs)

            engine.remember = remember
        return engine

    statuses: list[int] = []
    errors: list[BaseException] = []

    with _app(data, holding_factory) as client:
        _create(client, "w1")
        _create(client, "w2")
        _remember(client, "/api", "default keeps this")
        _remember(client, "/api/w/w1", "w1 before 1")
        _remember(client, "/api/w/w1", "w1 before 2")
        snap = client.post("/api/worlds/w1/snapshots", json={"note": "e2e"})
        assert snap.status_code == 200, snap.text
        snap_id = snap.json()["id"]
        for i in range(3):
            _remember(client, "/api/w/w1", f"w1 after {i}")
        assert _count(client, "/api/w/w1") == 5

        def writer():
            try:
                resp = client.post("/api/w/w2/remember", json={"text": "w2 held"})
                statuses.append(resp.status_code)
            except BaseException as exc:  # noqa: BLE001 — 交给主线程断言
                errors.append(exc)

        thread = threading.Thread(target=writer, name="w2-writer", daemon=True)
        try:
            thread.start()
            assert entered.wait(_JOIN_S), "w2 request never reached its engine"
            rolled = client.post("/api/worlds/w1/rollback",
                                 json={"snapshot_id": snap_id, "confirm": "w1",
                                       "mode": "discard_after"})
            # 回滚返回时 w2 的请求仍被扣在引擎里：两件事确实重叠。
            assert entered.is_set() and not release.is_set()
            assert thread.is_alive() and statuses == []
        finally:
            release.set()
            thread.join(_JOIN_S)
        assert not thread.is_alive(), "w2 writer did not finish"
        assert not errors, errors
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["reopened"] is True
        assert statuses == [200]

        for i in range(3):                                       # 放行后再来几条
            _remember(client, "/api/w/w2", f"w2 after {i}")
        assert _count(client, "/api/w/w2") == 4
        assert _count(client, "/api/w/w1") == 2                  # 回到快照内容
        assert _count(client, "/api/w/default") == 1             # 默认世界没被动
        assert _subjects(client, "/api/w/w1") == {"w1"}
        _remember(client, "/api/w/w1", "w1 again")               # 回滚后 w1 仍可写
        assert _count(client, "/api/w/w1") == 3


def _legacy_snapshot(db_path: Path, dest: Path) -> int:
    """真实的初始化过的世界库：先经 C++ 迁移建库，再 VACUUM INTO 成快照文件。"""
    adapter = _core.SqliteAdapter.open(str(db_path))
    del adapter
    gc.collect()
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return dest.stat().st_size


def _write_legacy_worlds_json(data: Path) -> str:
    """Task 2 之前的 worlds.json（形状同 tests/cpp/test_registry_import.cpp）：default 原地，
    sim-1 在 worlds/sim-1/ 下，带一份手动快照；库与快照文件都是真的。"""
    home = data / "worlds"
    sim_db = home / "sim-1" / "world.db"
    sim_db.parent.mkdir(parents=True)
    snap_id = "20261027T010000Z-aaaa"
    snap_path = home / "sim-1" / "snapshots" / f"{snap_id}.db"
    default_adapter = _core.SqliteAdapter.open(str(data / "dashboard.db"))   # 默认世界库：真实初始化
    del default_adapter
    gc.collect()
    size = _legacy_snapshot(sim_db, snap_path)
    legacy = {
        "version": 1, "default": "default", "active": "sim-1", "snapshot_retention": 5,
        "worlds": {
            "default": {"tenant": "default", "db_path": str(data / "dashboard.db"),
                        "snapshot_dir": str(home / "default" / "snapshots"),
                        "label": "主世界", "created_at": "2026-10-25T00:00:00Z",
                        "auto_snapshot": {"on_session": False, "interval_hours": 24}},
            "sim-1": {"tenant": "sim-1", "db_path": str(sim_db),
                      "snapshot_dir": str(home / "sim-1" / "snapshots"),
                      "label": "模拟一", "created_at": "2026-10-26T00:00:00Z",
                      "auto_snapshot": {"on_session": True, "interval_hours": 0}},
        },
        "snapshots": {"sim-1": [{
            "id": snap_id, "path": str(snap_path), "kind": "manual", "reason": "manual",
            "note": "旧快照", "created_at": "2026-10-27T01:00:00Z", "world_time": "",
            "size_bytes": size}]},
    }
    (data / "worlds.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    return snap_id


def _imported_copies(data: Path) -> list[str]:
    return sorted(p.name for p in data.glob("worlds.json.imported-*"))


def _snapshot_ids(client, name: str) -> list[str]:
    resp = client.get(f"/api/worlds/{name}/snapshots")
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["snapshots"]]


def test_restart_reads_imported_registry(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    snap_id = _write_legacy_worlds_json(data)

    with _app(data) as client:
        assert {"default", "sim-1"} <= _world_names(client)
        assert _snapshot_ids(client, "sim-1") == [snap_id]
        assert not (data / "worlds.json").exists()
        imported = _imported_copies(data)
        assert len(imported) == 1
        assert (data / "registry.db").exists()
        _create(client, "w3")
        before_names = _world_names(client)
        assert "w3" in before_names

    with _app(data) as client:                                   # 第二次启动：复用 registry.db
        assert _world_names(client) == before_names              # 世界行除本测试自己建的 w3 外没变
        assert _snapshot_ids(client, "sim-1") == [snap_id]
        assert _imported_copies(data) == imported
        assert not (data / "worlds.json").exists()
