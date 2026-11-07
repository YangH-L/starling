"""/api/v1/client 路由（记忆服务第 4 期 Task 4.5，spec §11、§13.1–§13.3；裁定 R46、R62、R63、R76–R78、R88）。

TestClient 跑 lifespan（有会话表）。池的时钟换成测试的时钟（R60：会话表的每次调用都从 pool.now_ms() 取）。抽取用 FakeLLMAdapter，
嵌入用占位，不发网络请求。
"""
import hashlib
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from starling import _core
from starling.dashboard import create_app
from starling.dashboard.config import DashboardConfig
from starling.dashboard.engine import DashboardEngine
from starling.dashboard.routes.world_scope import error_body, status_for

_STUB_JSON = ('[{"holder":"alice","holder_perspective":"FIRST_PERSON","subject":"Bob",'
              '"predicate":"responsible_for","object":"auth","modality":"BELIEVES",'
              '"polarity":"POS","nesting_depth":0}]')
SECRET_URL = "http://10.9.8.7:4242/secret-path"
SECRET_KEY = "sk-secret-key-0451"
API = "/api/v1/client"
FAKES: list = []


class _Clock:
    def __init__(self) -> None:
        self.now = int(time.monotonic() * 1000)

    def __call__(self) -> int:
        return self.now


def _stub_factory(ctx, providers):
    engine = DashboardEngine(ctx, providers)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB_JSON, True, "")
    FAKES.append(fake)
    engine.llm = fake
    return engine


def _cfg(tmp_path, **over):
    base = dict(db_path=str(tmp_path / "dashboard.db"), token="", tick_interval_s=600.0,
                source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"),
                providers={"p1": {"provider": "openai", "model": "m1", "base_url": SECRET_URL,
                                  "api_key": SECRET_KEY}},
                roles={"extraction": "p1"})
    base.update(over)
    return DashboardConfig(**base)


@pytest.fixture
def svc(tmp_path):
    FAKES.clear()
    app = create_app(_cfg(tmp_path), engine_factory=_stub_factory, idle_close_interval_s=0)
    with TestClient(app) as client:
        clock = _Clock()
        client.app.state.pool._monotonic_ms = clock
        client.app.state.pool.create_world("w1", "")
        yield client, clock


def _ok(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


def _err(resp, code):
    assert (resp.status_code, resp.json()) == (status_for(code), error_body(code)), resp.text


def _connect(client):
    return _ok(client.post(f"{API}/connect", json={"protocol": 1}))["session_id"]


def _open(client, sid, world="w1", holder="alice", read_only=False, **extra):
    return client.post(f"{API}/open", json={"session_id": sid, "world": world, "holder": holder,
                                            "read_only": read_only, **extra})


def _call(client, handle, method, **args):
    return client.post(f"{API}/call", json={"handle_id": handle, "method": method, "args": args})


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ───────────────────────── 会话与句柄 ─────────────────────────

def test_connect_returns_lease_heartbeat_and_info(svc):
    client, _clock = svc
    out = _ok(client.post(f"{API}/connect", json={"protocol": 1}))
    assert len(out["session_id"]) == 32
    assert (out["lease_ms"], out["heartbeat_ms"]) == (30000, 10000)
    assert out["info"] == _ok(client.post(f"{API}/info", json={}))


def test_connect_with_another_protocol_is_protocol_mismatch(svc):
    client, _clock = svc
    _err(client.post(f"{API}/connect", json={"protocol": 2}), "protocol_mismatch")


def test_round_trip_open_remember_recall_close(svc):
    client, _clock = svc
    sid = _connect(client)
    handle = _ok(_open(client, sid, holder="carol"))["handle_id"]
    out = _ok(_call(client, handle, "remember_event", key="k1", text="Bob owns auth",
                    interlocutor="bob", epoch="2026-01-01T00:00:00Z"))["result"]
    assert out["outcome"] == "accepted" and out["statement_ids"]
    assert _ok(_call(client, handle, "next_seq"))["result"] == 1
    assert _ok(_call(client, handle, "embed_pending", now="2026-01-01T00:00:05Z"))["result"]["embedded"] >= 1
    hits = _ok(_call(client, handle, "recall", query="Bob owns auth", k=10, mode="semantic",
                     strict=True, include_volatile=True))["result"]
    assert hits, "recall found nothing"
    hit = hits[0]
    assert set(hit) == {"content_id", "score", "row"}
    fields = {k for k, v in vars(_core.StatementRow).items() if isinstance(v, property)}
    assert set(hit["row"]) == fields                        # 整行（R77）
    assert hit["row"]["holder_id"] == "carol"               # 按句柄的 holder 写、按它召回（R76）
    ids = _ok(_call(client, handle, "content_ids", ids=out["statement_ids"]))["result"]
    assert ids[out["statement_ids"][0]] == hit["content_id"]
    digest = _ok(_call(client, handle, "digest"))["result"]
    assert digest == _ok(client.post(f"{API}/admin/digest", json={"world": "w1"}))["digest"]
    assert _ok(_call(client, handle, "pending_events"))["result"] == []
    _ok(client.post(f"{API}/close", json={"handle_id": handle, "timeout_s": 1.0}))
    _err(_call(client, handle, "digest"), "handle_closed")
    _ok(client.post(f"{API}/disconnect", json={"session_id": sid}))


def test_second_session_writable_open_is_world_held_read_only_is_fine(svc):
    client, _clock = svc
    first, second = _connect(client), _connect(client)
    _ok(_open(client, first))
    _err(_open(client, second), "world_held")
    reader = _ok(_open(client, second, read_only=True))["handle_id"]
    assert _ok(_call(client, reader, "next_seq"))["result"] == 0
    _err(_call(client, reader, "remember_event", key="k", text="t", interlocutor="bob"),
         "world_read_only")


def test_session_expired_after_31_seconds(svc):
    client, clock = svc
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    _ok(client.post(f"{API}/heartbeat", json={"session_id": sid}))
    clock.now += 31_000
    _err(_call(client, handle, "digest"), "session_expired")
    _err(client.post(f"{API}/heartbeat", json={"session_id": sid}), "session_expired")
    _err(_open(client, sid), "session_expired")
    other = _connect(client)
    _ok(_open(client, other))                              # 写占用已随过期释放


def test_failing_call_exits_and_releases_its_lease(svc):
    """R62：call_exit 在 finally 里，租约覆盖整个调用、调用结束就还。"""
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    _err(_call(client, handle, "abandon_event", key="no-such-key"), "event_not_found")
    assert sessions.in_flight(handle) == 0
    assert pool._leases.get("w1", 0) == 0
    _err(_call(client, handle, "no_such_method"), "invalid_request")
    assert sessions.in_flight(handle) == 0


def test_close_timeout_is_world_busy_and_the_handle_stays_open(svc):
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    FAKES[-1].set_delay_ms(700)
    box = {}

    def slow():
        box["resp"] = _call(client, handle, "remember_event", key="slow", text="Bob owns auth",
                            interlocutor="bob")
    worker = threading.Thread(target=slow)
    worker.start()
    deadline = time.monotonic() + 5
    while sessions.in_flight(handle) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sessions.in_flight(handle) == 1
    assert pool._leases.get("w1", 0) >= 1                   # R63：调用全程持租约
    _err(client.post(f"{API}/close", json={"handle_id": handle, "timeout_s": 0.1}), "world_busy")
    worker.join(10)
    assert box["resp"].status_code == 200, box["resp"].text
    FAKES[-1].set_delay_ms(0)
    assert _ok(_call(client, handle, "next_seq"))["result"] == 1   # 句柄还开着
    _ok(client.post(f"{API}/close", json={"handle_id": handle, "timeout_s": 1.0}))


def test_open_with_different_settings_is_world_settings_mismatch(svc):
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    stored = pool.record("w1")["settings"]
    sid = _connect(client)
    other = json.loads(json.dumps(stored))
    other["background"]["ingest"] = not stored["background"]["ingest"]
    _err(_open(client, sid, settings=other), "world_settings_mismatch")
    assert not sessions.world_write_held("w1", pool.now_ms())      # 什么都没登记
    _ok(_open(client, sid, settings=stored))
    _err(_open(client, _connect(client), read_only=True, settings={"api_key": "x"}),
         "invalid_settings")


# ───────────────────────── info ─────────────────────────

def _strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    else:
        yield str(value)


def test_info_has_no_key_or_address(svc):
    client, _clock = svc
    info = _ok(client.post(f"{API}/info", json={}))
    assert set(info) == {"version", "commit", "protocol", "extraction_config", "default_settings"}
    assert info["commit"] == _core.build_commit()
    assert info["version"] == _core.__version__ and info["protocol"] == 1
    assert info["extraction_config"]["belief_prompt"]
    assert info["default_settings"]["extraction"]["provider_name"] == "p1"
    for text in _strings(info):
        assert SECRET_URL not in text and SECRET_KEY not in text and "10.9.8.7" not in text, text
    assert "base_url" not in set(_strings(info)) and "api_key" not in set(_strings(info))


# ───────────────────────── 管理操作 ─────────────────────────

def test_admin_world_lifecycle(svc):
    client, _clock = svc
    pool = client.app.state.pool
    made = _ok(client.post(f"{API}/admin/create_world", json={"name": "s1", "labels": {"kind": "seed"},
                                                             "seed": True}))
    assert made["name"] == "s1" and made["seed"] is True and made["labels"] == {"kind": "seed"}
    fork = _ok(client.post(f"{API}/admin/fork", json={"source": "w1", "name": "r1", "reclaimable": True}))
    assert fork["world"]["name"] == "r1" and len(fork["digest"]) == 64
    up = _ok(client.post(f"{API}/admin/upgrade_seed", json={"source": "s1", "name": "s2"}))
    assert up["world"]["seed"] is True
    snap = _ok(client.post(f"{API}/admin/snapshot", json={"world": "w1", "note": "n"}))
    assert set(snap) == {"snapshot_id", "digest"}
    assert [s["kind"] for s in pool.snapshots("w1") if s["id"] == snap["snapshot_id"]] == ["held"]
    restored = _ok(client.post(f"{API}/admin/restore", json={"world": "w1",
                                                             "snapshot_id": snap["snapshot_id"]}))
    assert restored == {"digest": snap["digest"]}
    assert _ok(client.post(f"{API}/admin/pending_events", json={"world": "w1"})) == {"events": []}
    _ok(client.post(f"{API}/admin/close_world", json={"world": "w1"}))
    assert "w1" not in pool.open_worlds()
    _ok(client.post(f"{API}/admin/delete_snapshot", json={"world": "w1",
                                                          "snapshot_id": snap["snapshot_id"]}))
    _err(client.post(f"{API}/admin/delete_snapshot", json={"world": "w1",
                                                           "snapshot_id": snap["snapshot_id"]}),
         "snapshot_not_found")
    _err(client.post(f"{API}/admin/create_world", json={"name": "s1"}), "world_exists")
    _err(client.post(f"{API}/admin/digest", json={"world": "nope"}), "world_not_found")
    _err(client.post(f"{API}/admin/no_such_op", json={}), "invalid_request")


def test_list_worlds_with_digest_matches_and_leaves_closed_files_untouched(svc):
    client, _clock = svc
    pool = client.app.state.pool
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    _ok(_call(client, handle, "remember_event", key="k1", text="Bob owns auth", interlocutor="bob"))
    _ok(client.post(f"{API}/close", json={"handle_id": handle, "timeout_s": 1.0}))
    pool.create_world("w2", "", labels={"kind": "x"})
    pool.create_world("s1", "", seed=True)
    pool.close_world("w1")
    closed = {name: pool.record(name)["db_path"] for name in ("w1", "w2", "s1")}
    before = {name: _sha256(path) for name, path in closed.items()}
    seed_db = closed["s1"]
    assert not any(os.path.exists(seed_db + suffix) for suffix in ("-wal", "-shm"))
    page = _ok(client.post(f"{API}/admin/list_worlds", json={"page": 1, "page_size": 50,
                                                             "with_digest": True}))
    assert {name: _sha256(path) for name, path in closed.items()} == before   # 世界文件一个字节都没变
    # 种子由 C++ 判为可冻结（immutable）：只读打开不留旁车。普通世界的 mode=ro 共享打开照旧（R36）。
    assert not any(os.path.exists(seed_db + suffix) for suffix in ("-wal", "-shm"))
    assert set(pool.open_worlds()) == {"default"}                             # 算摘要不打开世界
    rows = {row["name"]: row for row in page["worlds"]}
    assert set(rows) == {"default", "w1", "w2", "s1"} and page["total"] == 4
    assert rows["w1"]["status"] == "closed" and rows["default"]["status"] == "open"
    assert rows["w1"]["statements"] >= 1 and rows["w2"]["labels"] == {"kind": "x"}
    for row in rows.values():
        assert {"name", "labels", "seed", "read_only", "fork_of_world", "fork_of_snapshot", "holders",
                "statements", "last_write_at", "status", "digest"} <= set(row)
    for name, row in rows.items():
        assert row["digest"] == _ok(client.post(f"{API}/admin/digest", json={"world": name}))["digest"]
    plain = _ok(client.post(f"{API}/admin/list_worlds", json={"labels": {"kind": "x"}, "page": 1,
                                                              "page_size": 1}))
    assert [row["name"] for row in plain["worlds"]] == ["w2"] and "digest" not in plain["worlds"][0]
    _err(client.post(f"{API}/admin/list_worlds", json={"page": 1, "page_size": 0}), "invalid_world_query")


# ───────────────────────── 错误码（R46） ─────────────────────────

@pytest.mark.parametrize("row", _core.error_catalog(), ids=lambda row: row["code"])
def test_every_catalog_code_maps_to_its_client_exception(row):
    """服务端的码表 → 客户端异常类：每个码（含本任务路由会回的全部码）都经 starling_client.errors.from_body 映射成对的类
    （R46），状态与错误体用服务端同一个构建函数（error_body / status_for）。客户端只认少数专名码，其余 4xx 是 WorldError、5xx 是
    ServiceError；transient 原样带过去。"""
    from starling_client import errors as E
    named = {"recall_degraded": E.RecallDegradedError, "event_conflict": E.EventConflict,
             "handle_closed": E.MemoryClosed, "protocol_mismatch": E.ProtocolMismatch,
             "session_expired": E.SessionExpired, "event_not_found": E.EventNotFound}
    code = row["code"]
    assert status_for(code) == row["http_status"]
    exc = E.from_body(status_for(code), error_body(code))
    want = named.get(code) or (E.ServiceError if row["http_status"] >= 500 else E.WorldError)
    assert type(exc) is want
    assert (exc.code, exc.transient) == (code, bool(row["transient"]))


# 本任务路由会回的码：都在 C++ 码表里（客户端测试 clients/python/tests/test_remote_memory.py 用同一份清单逐个映射）。
ROUTE_CODES = ("protocol_mismatch", "session_expired", "handle_closed", "world_held", "world_busy",
               "world_read_only", "world_not_found", "world_settings_mismatch", "invalid_settings",
               "invalid_request", "recall_degraded", "event_conflict", "event_not_found",
               "llm_not_configured", "worlds_unavailable", "world_open_failed", "endpoint_changed",
               "world_exists", "invalid_world_name", "world_schema_behind", "world_not_seed",
               "snapshot_not_found", "snapshot_corrupt", "restore_digest_mismatch",
               "fork_digest_mismatch", "insufficient_disk", "invalid_world_query", "world_protected",
               "labels_immutable", "invalid_labels", "snapshot_failed", "internal_error")


def test_route_codes_are_cataloged_except_internal_error():
    catalog = {row["code"] for row in _core.error_catalog()}
    assert set(ROUTE_CODES) - catalog == {"internal_error"}       # 兜底码，未登记 → 500 非瞬态
    assert (status_for("internal_error"), error_body("internal_error")["transient"]) == (500, False)
