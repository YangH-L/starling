"""WorldRegistry 绑定（registry.db）：建/查/列、设置继承、错误码、快照与回滚、GIL 释放。"""
import gc
import sqlite3
import threading
import time

import pytest

from starling import _core

NOW = "2026-11-03T00:00:00Z"


def _code(exc) -> str:
    return _core.world_store_error_code(exc)


def _open(tmp_path):
    return _core.WorldRegistry.open(str(tmp_path), str(tmp_path / "dashboard.db"), "default")


def _migrate(db_path):
    adapter = _core.SqliteAdapter.open(db_path)   # 跑迁移建库
    del adapter
    gc.collect()


def test_open_create_list_roundtrip(tmp_path):
    reg = _open(tmp_path)
    assert reg.data_dir == str(tmp_path)
    assert reg.default_world() == "default"
    a = reg.create("sim-a", "模拟甲", {"team": "x", "env": "dev"}, None, NOW)
    reg.create("sim-b", "", {"team": "y"}, None, "2026-11-03T00:00:01Z")
    assert a["tenant"] == "sim-a" and a["labels"] == {"team": "x", "env": "dev"}
    assert a["db_path"].endswith("worlds/" + a["dir_id"] + "/world.db")

    page = reg.list()
    assert page["total"] == 3 and {w["name"] for w in page["worlds"]} == {"default", "sim-a", "sim-b"}
    only_x = reg.list(labels={"team": "x"})
    assert [w["name"] for w in only_x["worlds"]] == ["sim-a"] and only_x["total"] == 1
    assert reg.list(offset=1, limit=1)["total"] == 3

    got = reg.get("sim-a")
    assert isinstance(got["settings"], dict) and got["settings"]["vector_backend"] == "sqlite"
    assert "settings_json" not in got
    reg.update_stats("sim-a", 2, 5, "2026-11-02T00:00:00Z", NOW)
    got = reg.get("sim-a")
    assert (got["holders"], got["statements"], got["last_write_at"]) == (2, 5, "2026-11-02T00:00:00Z")

    reg.remove("sim-a")
    assert reg.list()["total"] == 2


def test_create_inherits_default_settings_by_copy(tmp_path):
    reg = _open(tmp_path)
    ep = {"provider": "openai", "base_url": "http://h", "model": "m1", "dim": 8,
          "max_tokens": 100, "timeout_ms": 1000}
    merged = reg.update_settings("default", {"extraction": ep})
    assert merged["extraction"]["model"] == "m1"
    rec = reg.create("w1", "", {}, None, NOW)
    assert rec["settings"]["extraction"]["model"] == "m1"
    reg.update_settings("default", {"extraction": {"model": "m2"}})
    assert reg.get("default")["settings"]["extraction"]["model"] == "m2"
    assert reg.get("w1")["settings"]["extraction"]["model"] == "m1"
    # overrides 覆盖继承值
    rec2 = reg.create("w2", "", {}, {"extraction": {"model": "m3"}}, NOW)
    assert rec2["settings"]["extraction"]["model"] == "m3"
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.update_settings("w1", {"bogus": 1})
    assert _code(ei.value) == "invalid_settings"


def test_error_codes(tmp_path):
    reg = _open(tmp_path)
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.get("nope")
    assert _code(ei.value) == "world_not_found" and isinstance(ei.value, RuntimeError)
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.create("default", "", {}, None, NOW)
    assert _code(ei.value) == "world_exists"
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.remove("default")
    assert _code(ei.value) == "world_protected"
    with pytest.raises(_core.WorldStoreError) as ei:
        reg.create("Bad Name", "", {}, None, NOW)
    assert _code(ei.value) == "invalid_world_name"


def test_open_imports_legacy_worlds_json(tmp_path):
    (tmp_path / "worlds.json").write_text(
        '{"version":1,"default":"default","snapshot_retention":5,"worlds":{"default":'
        f'{{"tenant":"default","db_path":"{tmp_path}/dashboard.db",'
        f'"snapshot_dir":"{tmp_path}/worlds/default/snapshots","label":"主世界",'
        '"created_at":"2026-10-25T00:00:00Z"}},"snapshots":{}}')
    reg = _open(tmp_path)
    assert reg.get("default")["label"] == "主世界"
    assert not (tmp_path / "worlds.json").exists()


def test_helpers(tmp_path):
    assert _core.world_ingest_partition(True, "a b", "x") == "tenant-a%20b"
    assert _core.world_ingest_partition(False, "t", "abc123") == "world-abc123"
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m",
          "dim": 1, "max_tokens": 1, "timeout_ms": 1}
    _core.world_require_endpoint_unchanged(ep, "http://a")
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_require_endpoint_unchanged(ep, "http://b")
    assert _code(ei.value) == "endpoint_changed"
    _core.world_require_endpoint_unchanged({**ep, "provider_name": ""}, "http://b")
    _core.world_import_json(str(tmp_path))   # 无 worlds.json：空操作


def test_effective_settings_fills_cpp_defaults(tmp_path):
    """引擎池建引擎前要拿到实际生效的设置；默认值只在 C++（settings_from_json）。"""
    full = _core.world_effective_settings({})
    assert full["vector_backend"] == "sqlite"
    assert full["background"] == {"tick_interval_s": 30.0, "ingest": True,
                                  "auto_snapshot": {"on_session": False, "interval_hours": 24}}
    assert full["source_lane"]["enabled"] is True and "extraction" not in full
    part = _core.world_effective_settings({"extraction": {"model": "m"}})
    assert part["extraction"]["model"] == "m" and part["extraction"]["base_url"] == ""
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_effective_settings({"bogus": 1})
    assert _code(ei.value) == "invalid_settings"


def test_world_stats_reports_holders_and_last_write(tmp_path):
    reg = _open(tmp_path)
    rec = reg.create("sim-1", "", {}, None, NOW)
    missing = _core.world_stats(rec["db_path"], "sim-1")
    assert (missing["holders"], missing["last_write_at"]) == (0, "")
    _migrate(rec["db_path"])
    empty = _core.world_stats(rec["db_path"], "sim-1")
    assert (empty["holders"], empty["last_write_at"]) == (0, "")


def test_snapshot_and_rollback_on_registry(tmp_path):
    reg = _open(tmp_path)
    rec = reg.create("sim-1", "", {}, None, NOW)
    db = rec["db_path"]
    _migrate(db)
    stats = _core.world_stats(db, "sim-1")
    assert stats["engrams"] == 0 and stats["db_bytes"] > 0
    _core.world_verify_tenant(db, "sim-1")

    snap = _core.world_take_snapshot(reg, "sim-1", note="手动")["snapshot"]
    assert snap["kind"] == "manual" and snap["note"] == "手动"
    assert snap["sessions_complete"] is None or isinstance(snap["sessions_complete"], int)
    assert snap["session_id"] is None
    con = sqlite3.connect(f"file:{snap['path']}?mode=ro", uri=True)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert [s["id"] for s in reg.snapshots("sim-1")] == [snap["id"]]

    _core.world_check_rollback(reg, "sim-1", snap["id"])
    result = _core.world_rollback(reg, "sim-1", snap["id"])
    assert set(result) == {"archive_path", "restored_bytes"}
    assert len(_core.world_list_archives(reg, "sim-1")) == 1
    assert _core.world_purge_archives(reg, "sim-1") == 1
    assert _core.world_list_sessions(reg, "sim-1")["total"] == 0

    auto = _core.world_take_auto_snapshot(reg, "sim-1", "interval", NOW)
    assert set(auto) == {"snapshot", "skipped", "refused"}
    _core.world_set_auto_snapshot(reg, "sim-1", True, 0)
    assert reg.get("sim-1")["settings"]["background"]["auto_snapshot"] == {
        "on_session": True, "interval_hours": 0}
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_take_snapshot(reg, "sim-1", vector_backend="zvec")
    assert _code(ei.value) == "snapshot_unsupported_backend"

    _core.world_delete_snapshot(reg, "sim-1", snap["id"])
    assert reg.snapshots("sim-1") == []
    with pytest.raises(_core.WorldStoreError) as ei:
        _core.world_delete_snapshot(reg, "sim-1", snap["id"])
    assert _code(ei.value) == "snapshot_not_found"


def _progress_while(blocking_call) -> int:
    started, done = threading.Event(), threading.Event()

    def runner():
        started.set()
        try:
            blocking_call()
        finally:
            done.set()

    thread = threading.Thread(target=runner)
    thread.start()
    started.wait()
    progress = 0
    while not done.is_set():
        time.sleep(0.002)
        progress += 1
    thread.join()
    return progress


def test_registry_calls_release_gil(tmp_path):
    reg = _open(tmp_path)
    rec = reg.create("big", "", {}, None, NOW)
    _migrate(rec["db_path"])
    # before_vacuum 钩子在 C++ 里睡 300ms（GIL 已释放），窗口确定、不需要大库。
    progress = _progress_while(
        lambda: _core.world_take_snapshot(reg, "big", _test_before_vacuum_sleep_ms=300))
    assert progress >= 20, f"take_snapshot 期间主线程仅推进 {progress} 步——GIL 未释放"
