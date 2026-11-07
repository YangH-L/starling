"""世界错误的统一形状（记忆服务第 4 期 Task 4.1）：状态与瞬态标志取自 C++ 错误码目录，
响应体 {"code", "transient", "detail"}，detail 仍等于 code（前端 describeError 读 detail）。"""
import pytest
from fastapi.testclient import TestClient

from starling import _core, make_stub_llm
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine

# 4.1 之前两张手写 _STATUS 表的原样副本（routes/worlds.py、routes/world_scope.py）：基线，不随实现变。
_LEGACY_WORLDS = {
    "invalid_world_name": 400, "confirm_mismatch": 400, "rollback_mode_unsupported": 400,
    "invalid_auto_snapshot": 400, "invalid_settings": 400, "invalid_labels": 400,
    "invalid_world_query": 400,
    "world_not_found": 404, "snapshot_not_found": 404,
    "world_exists": 409, "world_protected": 409, "labels_immutable": 409,
    "snapshot_unsupported_backend": 409, "invalid_retention": 409, "endpoint_changed": 409,
    "world_tenant_mismatch": 409, "snapshot_corrupt": 409, "world_in_use": 409,
    "insufficient_disk": 507, "world_busy": 503, "worlds_unavailable": 503,
    "world_open_failed": 500, "snapshot_failed": 500, "registry_corrupt": 500,
    "rollback_reopen_failed": 500,
    "world_read_only": 409, "world_schema_behind": 409, "world_not_seed": 409,
    "fork_digest_mismatch": 500, "restore_digest_mismatch": 500,
}
_LEGACY_SCOPE = {
    "world_not_found": 404, "invalid_world_name": 400,
    "world_busy": 503, "worlds_unavailable": 503,
    "endpoint_changed": 409, "world_protected": 409, "world_tenant_mismatch": 409,
    "invalid_settings": 400,
    "world_open_failed": 500, "rollback_reopen_failed": 500,
    "world_read_only": 409, "world_schema_behind": 409, "world_not_seed": 409,
    "fork_digest_mismatch": 500, "restore_digest_mismatch": 500,
}


def _catalog() -> dict:
    return {row["code"]: row for row in _core.error_catalog()}


@pytest.mark.parametrize("legacy", [_LEGACY_WORLDS, _LEGACY_SCOPE], ids=["worlds", "world_scope"])
def test_catalog_status_matches_legacy_tables(legacy):
    catalog = _catalog()
    for code, status in legacy.items():
        assert code in catalog, code
        assert catalog[code]["http_status"] == status, code


def test_route_tables_are_built_from_catalog():
    from starling.dashboard.routes import world_scope, worlds
    catalog = _catalog()
    for table in (world_scope._STATUS, worlds._STATUS):
        assert table == {code: row["http_status"] for code, row in catalog.items()}


def _stub_factory(ctx, providers):
    engine = DashboardEngine(ctx, providers)
    engine.llm = make_stub_llm(default_response="[]")
    return engine


@pytest.fixture
def client(tmp_path):
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0, source_lane_enabled=False,
                          ingest_spool_path=str(tmp_path / "spool"))
    with TestClient(create_app(cfg, engine_factory=_stub_factory)) as test_client:
        yield test_client


def test_world_error_body_has_code_and_transient(client):
    # 作用域路由（异常处理器路径）与 /api/worlds 路由（_fail 路径）两条出口。
    scoped = client.get("/api/w/nope/overview")
    assert scoped.status_code == 404
    assert scoped.json() == {"code": "world_not_found", "transient": False,
                             "detail": "world_not_found"}
    worlds = client.get("/api/worlds/nope")
    assert worlds.status_code == 404
    assert worlds.json() == {"code": "world_not_found", "transient": False,
                             "detail": "world_not_found"}
    bad = client.post("/api/worlds", json={"name": "Bad Name!"})
    assert bad.status_code == 400
    assert bad.json() == {"code": "invalid_world_name", "transient": False,
                          "detail": "invalid_world_name"}
