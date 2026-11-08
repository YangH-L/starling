import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from starling.dashboard import DashboardConfig, create_app


def _seed(db_path: str):
    """Build the schema via the runtime, then raw-seed a few rows, commit+close."""
    from pathlib import Path
    from starling import runtime as rt
    r = rt._build_local_store_sqlite_runtime(Path(db_path))
    r.start()
    # ensure WAL is flushed and the writer handle is released before raw seeding
    del r
    conn = sqlite3.connect(db_path)
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO statements (id, tenant_id, holder_id, holder_perspective, "
        "subject_kind, subject_id, predicate, object_kind, object_value, "
        "canonical_object_hash, canonical_object_hash_version, modality, polarity, "
        "confidence, observed_at, salience, affect_json, activation, last_accessed, "
        "provenance, created_at, updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "default", "self", "first_person", "cognizer", "Bob", "responsible_for",
         "str", "auth", "h", "v1", "BELIEVES", "POS", 0.9, "2026-04-15T10:00:00Z",
         0.5, "{}", 0.0, "2026-04-15T10:00:00Z", "test",
         "2026-04-15T10:00:00Z", "2026-04-15T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO commitments (stmt_id, tenant_id, state, broken_count, deadline, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (sid, "default", "ACTIVE", 0, "2026-04-20T10:00:00Z",
         "2026-04-15T10:00:00Z", "2026-04-15T10:00:00Z"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "dash.db")
    _seed(db)
    cfg = DashboardConfig(db_path=db, token="")
    return TestClient(create_app(cfg))


def test_overview(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["statements"] >= 1
    assert body["commitments_by_state"].get("ACTIVE") == 1


def test_statements_filter(client):
    r = client.get("/api/statements", params={"predicate": "responsible_for"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows and rows[0]["predicate"] == "responsible_for"
    # QA ISSUE-001 回归:详情抽屉要能看到治理字段(谓词注册表/校验器的降级
    # 结果),读侧必须带出这三列。
    for field in ("review_status", "consolidation_state", "nesting_depth"):
        assert field in rows[0], f"statements 读侧缺 {field}"


def test_commitments_joins_statement(client):
    r = client.get("/api/commitments")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows and rows[0]["state"] == "ACTIVE" and rows[0]["object_value"] == "auth"


def _copy_statement_to_other_tenant(conn, sid, object_value):
    original = conn.execute(
        "SELECT * FROM statements WHERE tenant_id='default' AND id=?", (sid,)
    ).fetchone()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(statements)")]
    other = dict(zip(columns, original))
    other["tenant_id"] = "other"
    other["object_value"] = object_value
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO statements({','.join(columns)}) VALUES({placeholders})",
        tuple(other[c] for c in columns),
    )


def test_commitments_join_is_tenant_scoped(client):
    db_path = client.app.state.config.db_path
    conn = sqlite3.connect(db_path)
    sid = conn.execute(
        "SELECT stmt_id FROM commitments WHERE tenant_id='default'"
    ).fetchone()[0]
    _copy_statement_to_other_tenant(conn, sid, "secret-other-tenant")
    conn.commit()
    conn.close()

    rows = client.get("/api/commitments").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["object_value"] == "auth"


def test_embedding_backlog_join_is_tenant_scoped(client):
    db_path = client.app.state.config.db_path
    conn = sqlite3.connect(db_path)
    sid = conn.execute(
        "SELECT id FROM statements WHERE tenant_id='default'"
    ).fetchone()[0]
    _copy_statement_to_other_tenant(conn, sid, "other")
    conn.execute(
        "INSERT INTO statement_vectors(tenant_id,stmt_id,dim,model,status) "
        "VALUES('other',?,1,'test','embedded')",
        (sid,),
    )
    conn.commit()
    conn.close()

    assert client.get("/api/queues").json()["embedding_backlog"] == 1


def test_replay_conflicts_queues_shape(client):
    assert client.get("/api/replay").status_code == 200
    assert "by_kind" in client.get("/api/conflicts").json()
    assert "embedding_backlog" in client.get("/api/queues").json()


def test_eval_reports(client):
    r = client.get("/api/eval")
    assert r.status_code == 200 and isinstance(r.json()["reports"], list)


def test_statement_by_id_roundtrip(client):
    # Use the pre-seeded statement (via _seed) instead of /api/remember
    # (which needs LLM); fetch its id from the list endpoint first.
    rows = client.get("/api/statements").json()["rows"]
    sid = rows[0]["id"]
    r = client.get(f"/api/statement/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid and body["consolidation_state"] in (
        "volatile", "consolidated", "archived")


def test_statement_by_id_404_when_absent(client):
    r = client.get("/api/statement/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"] == "not_found"
    # 最后修复波 A6（裁定 R48）：码表的错误体。
    assert r.json() == {"code": "not_found", "transient": False, "detail": "not_found"}


@pytest.mark.parametrize("path", ["/api/statement/nope", "/api/provenance/nope",
                                  "/api/cascade_preview/nope"])
def test_missing_statement_routes_use_the_catalog_not_found_body(client, path):
    """最后修复波 A6（裁定 R48）：检视路由找不到行时 404 用码表的错误体，detail 仍是 not_found（前端读 detail）。"""
    r = client.get(path)
    assert (r.status_code, r.json()) == (404, {"code": "not_found", "transient": False,
                                               "detail": "not_found"})
