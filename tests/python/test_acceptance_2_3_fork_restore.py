"""第 3 期验收（spec §16 第 2、3 条）与只读召回不改字节。

2：种子分叉出的副本与种子摘要相同，写其中一个副本不影响种子和其他副本，种子文件字节不变。
3：快照 → 写入 → 恢复，摘要等于快照时的值。（「有句柄开着时恢复返回 world_held」属第 4 期写占用，裁定 R25。）
只读：左脑 lane 生效时（裁定 R24），可回收世界的召回前后 world.db 字节不变，没有新的 -wal/-shm。
"""
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from starling import _core
from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine

_STUB = ('[{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob",'
         '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
         '"polarity":"POS","nesting_depth":0}]')


def _stub_factory(ctx, providers):
    engine = DashboardEngine(ctx, providers)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB, True, "")
    engine.llm = fake
    return engine


def _make_client(tmp_path, **cfg_kw):
    cfg = DashboardConfig(db_path=str(tmp_path / "dashboard.db"), token="",
                          tick_interval_s=600.0,
                          ingest_spool_path=str(tmp_path / "spool"), **cfg_kw)
    return TestClient(create_app(cfg, engine_factory=_stub_factory))


@pytest.fixture
def client(tmp_path):
    with _make_client(tmp_path, source_lane_enabled=False) as test_client:
        yield test_client


@pytest.fixture
def lane_client(tmp_path):
    # 离线嵌入是 StubEmbeddingAdapter(8)，model 为 "stub"；tau 用默认 0.56。
    with _make_client(tmp_path, source_lane_enabled=True, source_lane_model="stub",
                      source_lane_dim=8) as test_client:
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


def _write(client, name: str, text: str) -> None:
    resp = client.post(f"/api/w/{name}/remember", json={"text": text})
    assert resp.status_code == 200, resp.text
    assert client.post(f"/api/w/{name}/tick", json={}).status_code == 200


def _source_with_content(client, name: str = "src") -> dict:
    rec = _pool(client).create_world(name, "")
    _write(client, name, "Bob owns auth")
    return rec


def _fork(client, source: str, new_name: str, **body) -> dict:
    resp = client.post(f"/api/worlds/{source}/fork", json={"new_name": new_name, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _dig(client, name: str) -> str:
    rec = _pool(client).record(name)
    return _digest(rec["db_path"], rec["tenant"])


def test_2_seed_forks_share_digest_and_seed_bytes_unchanged(client):
    pool = _pool(client)
    _source_with_content(client)
    seed_out = _fork(client, "src", "seed-1", seed=True)
    seed_db = pool.record("seed-1")["db_path"]
    seed_sha = _sha256(seed_db)
    seed_digest = _dig(client, "seed-1")
    assert seed_out["digest"] == seed_digest == _dig(client, "src")

    c1 = _fork(client, "seed-1", "c1")
    c2 = _fork(client, "seed-1", "c2")
    assert c1["digest"] == c2["digest"] == seed_digest
    assert _dig(client, "c1") == _dig(client, "c2") == seed_digest
    c2_db = pool.record("c2")["db_path"]
    c2_sha = _sha256(c2_db)

    _write(client, "c1", "Alice owns billing")
    assert _dig(client, "c1") != seed_digest
    assert _dig(client, "seed-1") == seed_digest
    assert _dig(client, "c2") == seed_digest
    assert _sha256(seed_db) == seed_sha
    assert _sha256(c2_db) == c2_sha                    # 写 c1 不碰另一个副本的字节


def test_3_snapshot_write_restore_digest(client):
    _source_with_content(client)
    snap = client.post("/api/worlds/src/snapshots", json={"note": "acc3"})
    assert snap.status_code == 200, snap.text
    snap = snap.json()
    before = _dig(client, "src")
    # 快照路由返回 digest（裁定 R26），等于拍快照时的库摘要。
    assert snap["digest"] == before

    _write(client, "src", "Alice owns billing")
    assert _dig(client, "src") != before

    resp = client.post("/api/worlds/src/rollback",
                       json={"snapshot_id": snap["id"], "confirm": "src",
                             "mode": "discard_after"})
    assert resp.status_code == 200, resp.text
    # 恢复路由返回 C++ 在恢复后的库上算的 digest，等于快照返回的那个，也等于此刻库的摘要。
    assert resp.json()["digest"] == snap["digest"]
    assert _dig(client, "src") == before


def test_read_only_recall_keeps_sha256(lane_client):
    client = lane_client
    pool = _pool(client)
    _source_with_content(client)
    _fork(client, "src", "rollout-1", reclaimable=True)
    db = pool.record("rollout-1")["db_path"]
    sha, files = _sha256(db), _dir_files(db)

    resp = client.post("/api/w/rollout-1/recall", json={"query": "who owns auth"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"], "召回要真的读到分叉过来的语句"

    with pool.lease("rollout-1", build=False) as engine:
        assert engine.read_only is True
        assert engine.source_lane_status()["active"] is True    # R24：lane 真的生效
    pool.close_world("rollout-1")

    assert _sha256(db) == sha
    after = _dir_files(db)
    assert after == files, after - files
    assert not any(n.endswith(("-wal", "-shm")) for n in after)
