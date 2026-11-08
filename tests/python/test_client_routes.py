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
    assert set(info) == {"version", "commit", "protocol", "extraction_config", "default_settings",
                         "key_set"}                                  # key_set：裁定 R101
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
             "session_expired": E.SessionExpired, "event_not_found": E.EventNotFound,
             "unauthorized": E.Unauthorized}
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
               "endpoint_key_missing",
               "world_exists", "invalid_world_name", "world_schema_behind", "world_not_seed",
               "snapshot_not_found", "snapshot_corrupt", "restore_digest_mismatch",
               "fork_digest_mismatch", "insufficient_disk", "invalid_world_query", "world_protected",
               "labels_immutable", "invalid_labels", "snapshot_failed", "internal_error",
               "unauthorized")


def test_route_codes_are_all_cataloged():
    """最后修复波 A7（裁定 R100 M1）：兜底码 internal_error 也在码表里（500，非瞬态），没有例外。"""
    catalog = {row["code"]: row for row in _core.error_catalog()}
    assert set(ROUTE_CODES) - set(catalog) == set()
    assert (catalog["internal_error"]["http_status"], catalog["internal_error"]["transient"]) == (500, False)
    assert (status_for("internal_error"), error_body("internal_error")["transient"]) == (500, False)


# ───────────────────────── 修复轮 1 F1：抽取期间不持引擎锁 ─────────────────────────

def test_same_world_recall_is_not_blocked_by_remember_event_extraction(svc):
    """F1：客户端 remember_event 只在 begin 与 finish 持引擎锁，抽取（每次 LLM 调用 700 ms）在锁外。同一世界另一个句柄的
    recall 在 200 ms 内返回，不排在抽取后面。"""
    client, _clock = svc
    sessions = client.app.state.sessions
    sid = _connect(client)
    writer = _ok(_open(client, sid, holder="alice"))["handle_id"]
    reader = _ok(_open(client, sid, holder="bob"))["handle_id"]
    assert _ok(_call(client, reader, "next_seq"))["result"] == 0
    FAKES[-1].set_delay_ms(700)
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("resp", _call(
        client, writer, "remember_event", key="slow", text="Bob owns auth", interlocutor="bob")))
    worker.start()
    deadline = time.monotonic() + 5
    while sessions.in_flight(writer) == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)                                      # 进到抽取里
    started = time.monotonic()
    resp = _call(client, reader, "recall", query="x", strict=False, include_volatile=True)
    waited = time.monotonic() - started
    worker.join(15)
    FAKES[-1].set_delay_ms(0)
    assert resp.status_code == 200, resp.text
    assert waited < 0.2, f"recall waited {waited * 1000:.0f} ms behind the extraction"
    assert box["resp"].status_code == 200, box["resp"].text
    assert box["resp"].json()["result"]["outcome"] == "accepted"


# ───────────────────────── 修复轮 1 F2：KeyError 只在 abandon_event 上是 event_not_found ─────────────────────────

def test_key_error_outside_abandon_event_is_internal_error(svc, monkeypatch):
    """F2：只有 abandon_event 的 KeyError 是 404 event_not_found。别的方法里的 KeyError（这里是缺 content_id 的命中）是服务端的
    bug，回 500 internal_error。"""
    client, _clock = svc
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    monkeypatch.setattr(DashboardEngine, "recall", lambda self, *a, **k: [{"row": None, "score": 0.0}])
    _err(_call(client, handle, "recall", query="q"), "internal_error")
    monkeypatch.setattr(DashboardEngine, "next_seq", lambda self: (_ for _ in ()).throw(ValueError("x")))
    _err(_call(client, handle, "next_seq"), "internal_error")
    _err(_call(client, handle, "abandon_event", key="nope"), "event_not_found")
    _err(_call(client, handle, "recall", query=7), "invalid_request")       # 参数校验仍是 400


# ───────────────────────── 修复轮 1 F3：holder 为空回 400 ─────────────────────────

@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "read_only"])
def test_open_with_an_empty_holder_is_invalid_request(svc, read_only):
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = _connect(client)
    _err(_open(client, sid, holder="", read_only=read_only), "invalid_request")
    assert not sessions.world_has_live_handles("w1", pool.now_ms())


# ───────────────────────── 修复轮 1 F4：复审时存活的 4 个变异 ─────────────────────────

def test_recall_degraded_is_503_transient_with_the_default_strict(svc, monkeypatch):
    """strict 缺省 True：查询词嵌入失败时 503 recall_degraded，transient=True。用真的引擎与失败的嵌入器。"""
    client, _clock = svc
    import starling
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    pool = client.app.state.pool
    with pool.lease("w1") as engine:
        engine._core.set_embedder(starling.make_openai_embedder(
            model="offline", dim=8, timeout_ms=10, base_url="unsupported-starling-test://invalid"))
        engine = None
    resp = _call(client, handle, "recall", query="anything")
    assert (resp.status_code, resp.json()) == (503, {"code": "recall_degraded", "transient": True,
                                                     "detail": "recall_degraded"})
    assert _ok(_call(client, handle, "recall", query="anything", strict=False))["result"] == []


def test_recall_forwards_strict_and_include_volatile_to_the_engine(svc, monkeypatch):
    client, _clock = svc
    seen = []
    real = DashboardEngine.recall

    def spy(self, *args, **kwargs):
        seen.append((kwargs["strict"], kwargs["include_volatile"], kwargs["holder"]))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(DashboardEngine, "recall", spy)
    sid = _connect(client)
    handle = _ok(_open(client, sid, holder="carol"))["handle_id"]
    _ok(_call(client, handle, "recall", query="q"))
    _ok(_call(client, handle, "recall", query="q", strict=False, include_volatile=False))
    assert seen == [(True, True, "carol"), (False, False, "carol")]


def test_remember_event_without_an_extraction_role_is_llm_not_configured(tmp_path):
    FAKES.clear()

    def no_llm(ctx, providers):
        engine = DashboardEngine(ctx, providers)
        engine.llm = None
        return engine
    app = create_app(_cfg(tmp_path, roles={}), engine_factory=no_llm, idle_close_interval_s=0)
    with TestClient(app) as client:
        client.app.state.pool.create_world("w1", "")
        sid = _connect(client)
        handle = _ok(_open(client, sid))["handle_id"]
        _err(_call(client, handle, "remember_event", key="k", text="t", interlocutor="bob"),
             "llm_not_configured")
        assert _ok(_call(client, handle, "pending_events"))["result"] == []   # 写 pending 行之前就拒绝


def test_facade_calls_count_as_world_activity(svc):
    """门面调用的租约 touch=True（R80）：每次调用都把世界的最后活动推到「此刻」。会话按 10 s 一次心跳活着（心跳本身不算世界
    活动），句柄一直开着；关掉句柄之后，空闲判定从最后一次调用算起。"""
    client, clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    idle = 600_000                                     # SessionTable 缺省 idle_close_ms
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]      # open_handle 记一次活动

    def advance(ms):
        while ms > 0:
            step = min(ms, 10_000)
            clock.now += step
            ms -= step
            _ok(client.post(f"{API}/heartbeat", json={"session_id": sid}))

    advance(idle - 10_000)
    _ok(_call(client, handle, "next_seq"))             # 门面调用：活动推到此刻
    sessions.close_handle(handle)
    advance(idle - 10_000)                              # 距 open 已远超 idle，距调用还差 10 s
    assert not sessions.may_close_world("w1", False, pool.now_ms())
    advance(20_000)
    assert sessions.may_close_world("w1", False, pool.now_ms())


def test_facade_call_lease_touches_the_world(svc, monkeypatch):
    """R80：门面调用取的是算活动的租约（touch=True），经池记一次 touch_world。SessionTable 的 call_enter / call_exit 也记活动，
    所以光看 may_close_world 分不出租约是否 touch；这里直接看池的 touch。"""
    client, _clock = svc
    pool = client.app.state.pool
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    touched = []
    real = pool._touch
    monkeypatch.setattr(pool, "_touch", lambda world: (touched.append(world), real(world)))
    _ok(_call(client, handle, "next_seq"))
    assert touched == ["w1"]


# ───────────────────────── 修复轮 1 F6：继承了端点的世界，open(settings=…) 用得上 ─────────────────────────

def test_open_with_settings_seen_by_the_client_matches_an_inherited_endpoint(svc):
    """F6：世界继承了 provider p1 的抽取端点，存着的设置里有 base_url（服务端从 provider 表补进来），info 不给它。客户端用它看得到的
    设置（info.default_settings 加上建世界时的 overrides）去 open，视为相同；只改 model 是 mismatch；只改 base_url 视为相同。"""
    client, _clock = svc
    overrides = {"background": {"ingest": False}}
    _ok(client.post(f"{API}/admin/create_world", json={"name": "r1", "settings": overrides}))
    info = _ok(client.post(f"{API}/info", json={}))
    assert "base_url" not in info["default_settings"]["extraction"]
    stored = client.app.state.pool.record("r1")["settings"]
    assert stored["extraction"]["base_url"] == SECRET_URL            # 存着的确实带地址
    seen = json.loads(json.dumps(info["default_settings"]))
    seen["background"]["ingest"] = False
    sid = _connect(client)
    _ok(_open(client, sid, world="r1", read_only=True, settings=seen))
    other_url = json.loads(json.dumps(seen))
    other_url["extraction"]["base_url"] = "http://elsewhere/v1"
    _ok(_open(client, sid, world="r1", read_only=True, settings=other_url))
    other_model = json.loads(json.dumps(seen))
    other_model["extraction"]["model"] = "m2"
    _err(_open(client, sid, world="r1", read_only=True, settings=other_model), "world_settings_mismatch")


# ───────────────────────── 修复轮 2 N1：同一个 key 并发、写入途中被放弃 ─────────────────────────

def _statements_and_receipts(pool, world):
    import sqlite3
    con = sqlite3.connect(f"file:{pool.record(world)['db_path']}?mode=ro", uri=True)
    try:
        ids = {row[0] for row in con.execute("SELECT id FROM statements")}
        receipts = {key: (state, json.loads(sids or "[]"))
                    for key, state, sids in con.execute("SELECT key, state, statement_ids_json FROM events")}
    finally:
        con.close()
    return ids, receipts


def test_concurrent_replay_of_one_key_converges_without_orphans(svc):
    """N1：客户端超时后按 key 重放。第一次请求在假 LLM 里阻塞时第二次请求到达；两次都 200，后完成的那次是 idempotent，
    两次的 statement_ids 相同，语句表里没有不在收据里的语句。"""
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    FAKES[-1].set_delay_ms(300)
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("first", _call(
        client, handle, "remember_event", key="k", text="Bob owns auth", interlocutor="bob")))
    worker.start()
    deadline = time.monotonic() + 5
    while sessions.in_flight(handle) == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)                                            # 第一次已进到抽取里（它在第 1 相锁内取好了适配器）
    # 重放的那次换一个适配器、给出不同的抽取结果（生产里重抽多半不逐字相同；相同时 span key 去重会掩盖孤儿）。不在第一次
    # 抽取途中改它的适配器：FakeLLMAdapter 不带锁。
    replay_llm = _core.FakeLLMAdapter()
    replay_llm.set_default_response(_STUB_JSON.replace('"auth"', '"billing"'), True, "")
    with pool.lease("w1") as engine:
        engine.llm = replay_llm
        engine = None
    second = _call(client, handle, "remember_event", key="k", text="Bob owns auth", interlocutor="bob")
    worker.join(20)
    FAKES[-1].set_delay_ms(0)
    first = box["first"]
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    # 重放的那次没有延迟，先跑完、写出语句（outcome 是 idempotent：第一次的 begin 已写好 engram，证据重入；语句是新写的）。
    # 被阻塞的第一次在 finish 里重读到 committed：idempotent，拿重放那次的语句，自己的 bundle 不提交。
    assert first.json()["result"]["outcome"] == "idempotent"
    assert second.json()["result"]["outcome"] in ("accepted", "idempotent")
    assert second.json()["result"]["statement_ids"]
    assert first.json()["result"]["statement_ids"] == second.json()["result"]["statement_ids"]
    ids, receipts = _statements_and_receipts(pool, "w1")
    assert receipts["k"][0] == "committed"
    assert ids == set(receipts["k"][1]) and ids                    # 没有孤儿


def test_abandon_during_a_write_wins_and_leaves_no_statements(svc):
    """N1：写入途中 key 被放弃。abandon 200；remember_event 409 event_conflict；这个 key 下没有语句。"""
    client, _clock = svc
    pool, sessions = client.app.state.pool, client.app.state.sessions
    sid = _connect(client)
    writer = _ok(_open(client, sid))["handle_id"]
    FAKES[-1].set_delay_ms(300)
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("write", _call(
        client, writer, "remember_event", key="k", text="Bob owns auth", interlocutor="bob")))
    worker.start()
    deadline = time.monotonic() + 5
    while sessions.in_flight(writer) == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)
    assert _call(client, writer, "abandon_event", key="k").status_code == 200
    worker.join(20)
    FAKES[-1].set_delay_ms(0)
    _err(box["write"], "event_conflict")
    ids, receipts = _statements_and_receipts(pool, "w1")
    assert receipts["k"] == ("abandoned", [])
    assert ids == set()


# ───────────────────────── 修复轮 2 N2、N3：recall 的参数校验 ─────────────────────────

def test_recall_negative_k_is_invalid_request_and_zero_is_fine(svc):
    client, _clock = svc
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    _err(_call(client, handle, "recall", query="q", k=-1), "invalid_request")
    assert _ok(_call(client, handle, "recall", query="q", k=0))["result"] == []


def test_completion_mode_needs_strict_and_include_volatile_off(svc):
    """R93(3)：completion 模式没有降级信号，客户端缺省的 strict=True / include_volatile=True 与它不兼容，在调引擎之前回 400
    invalid_request（非瞬态）；两者都传 False 时 200。"""
    client, _clock = svc
    sid = _connect(client)
    handle = _ok(_open(client, sid))["handle_id"]
    resp = _call(client, handle, "recall", query="q", mode="completion")
    assert (resp.status_code, resp.json()) == (400, {"code": "invalid_request", "transient": False,
                                                     "detail": "invalid_request"})
    _err(_call(client, handle, "recall", query="q", mode="completion", strict=False), "invalid_request")
    _ok(_call(client, handle, "recall", query="q", mode="completion", strict=False, include_volatile=False))


# ───────────────────────── 最后修复波 A1：显式端点缺服务密钥 → endpoint_key_missing ─────────────────────────

# 客户端显式给的端点（没有 provider_name）。地址是本机一个不监听的端口：万一真发出请求也只会连不上，不会走到外网。
_EXPLICIT = {"extraction": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m-x"},
             "embedding": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "e-x",
                           "dim": 8}}


@pytest.fixture
def no_key_svc(tmp_path, monkeypatch):
    """服务环境里没有 OPENAI_API_KEY；默认世界没配 provider（看板「没有配 provider 的默认世界」）。引擎是真的
    DashboardEngine，不注入抽取替身：替身不是从设置建的，会绕过这道判定。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(_cfg(tmp_path, providers={}, roles={}), idle_close_interval_s=0)
    with TestClient(app) as client:
        client.app.state.pool._monotonic_ms = _Clock()
        yield client


def test_explicit_endpoint_without_the_service_key_is_endpoint_key_missing(no_key_svc):
    """裁定 R100 I1（spec §8.3）：显式端点、服务环境里没有密钥：remember_event、embed_pending、recall(strict=True) 都回 409
    endpoint_key_missing（非瞬态），不退回占位嵌入、也不当成「抽取没绑定」。remember_event 在写 pending 行之前就拒绝。"""
    client = no_key_svc
    _ok(client.post(f"{API}/admin/create_world", json={"name": "x1", "settings": _EXPLICIT}))
    sid = _connect(client)
    handle = _ok(_open(client, sid, world="x1"))["handle_id"]
    resp = _call(client, handle, "remember_event", key="k", text="Bob owns auth", interlocutor="bob")
    assert (resp.status_code, resp.json()) == (409, {"code": "endpoint_key_missing", "transient": False,
                                                     "detail": "endpoint_key_missing"})
    _err(_call(client, handle, "embed_pending", now="2026-01-01T00:00:05Z"), "endpoint_key_missing")
    _err(_call(client, handle, "recall", query="auth", strict=True, include_volatile=True),
         "endpoint_key_missing")
    _err(_call(client, handle, "recall", query="auth", strict=False), "endpoint_key_missing")
    assert _ok(_call(client, handle, "pending_events"))["result"] == []
    assert _ok(_call(client, handle, "next_seq"))["result"] == 0


def test_the_default_world_without_a_provider_is_unchanged(no_key_svc):
    """没配 provider 的默认世界照旧：嵌入用占位（embed_pending、strict 召回都 200），抽取没绑定是 llm_not_configured。"""
    client = no_key_svc
    sid = _connect(client)
    handle = _ok(_open(client, sid, world="default"))["handle_id"]
    _ok(_call(client, handle, "embed_pending", now="2026-01-01T00:00:05Z"))
    assert _ok(_call(client, handle, "recall", query="auth", strict=True))["result"] == []
    _err(_call(client, handle, "remember_event", key="k", text="t", interlocutor="bob"), "llm_not_configured")


# ───────────────────────── 最后修复波 A5（裁定 R91(5)）：401 也用码表的错误体 ─────────────────────────

def test_wrong_or_missing_token_is_the_catalog_unauthorized_body(tmp_path):
    """token 错或没带：401 {"code":"unauthorized","transient":false,"detail":"invalid or missing bearer token"}。detail 保留原来的
    文字（前端只按状态码判 401，显示的是 detail）；体里没有 token。"""
    app = create_app(_cfg(tmp_path, token="tok-secret-0451"), engine_factory=_stub_factory,
                     idle_close_interval_s=0)
    with TestClient(app) as client:
        want = {"code": "unauthorized", "transient": False, "detail": "invalid or missing bearer token"}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "tok-secret-0451"}):
            resp = client.post(f"{API}/connect", json={"protocol": 1}, headers=headers)
            assert (resp.status_code, resp.json()) == (401, want)
            assert "tok-secret-0451" not in resp.text
        resp = client.get("/api/ping")
        assert (resp.status_code, resp.json()) == (401, want)
        assert status_for("unauthorized") == 401
        ok = client.post(f"{API}/connect", json={"protocol": 1},
                         headers={"Authorization": "Bearer tok-secret-0451"})
        assert ok.status_code == 200


# ───────────────────────── 最后修复波 A10（裁定 R101，spec §8.2）：info.key_set 按角色给出 ─────────────────────────

def test_info_key_set_is_per_role_and_carries_no_key_or_address(tmp_path, monkeypatch):
    """info 在 default_settings 旁边多一个 key_set: {role: bool}，四个角色（有 endpoint 的才出现）。规则是 engine.endpoint_api_key
    （与看板世界详情的 _public_settings 同一条）：继承 provider 的按该 provider 此刻的密钥，现算不缓存。default_settings 不带
    key_set。遍历 info 的 JSON：没有密钥、没有地址。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    other_url = "http://10.9.8.8:4343/other-path"
    providers = {"p1": {"provider": "openai", "model": "m1", "base_url": SECRET_URL, "api_key": SECRET_KEY},
                 "p2": {"provider": "openai", "model": "e1", "base_url": other_url, "dim": 8},
                 "p3": {"provider": "openai", "model": "j1", "base_url": other_url}}
    app = create_app(_cfg(tmp_path, providers=providers,
                          roles={"extraction": "p1", "embedding": "p2", "chat": "p1", "consolidation": "p3"}),
                     engine_factory=_stub_factory, idle_close_interval_s=0)
    with TestClient(app) as client:
        info = _ok(client.post(f"{API}/info", json={}))
        assert info["key_set"] == {"extraction": True, "embedding": False, "chat": True, "consolidation": False}
        assert all("key_set" not in section for section in info["default_settings"].values()
                   if isinstance(section, dict))
        for text in _strings(info):
            for secret in (SECRET_URL, SECRET_KEY, other_url, "10.9.8.7", "10.9.8.8"):
                assert secret not in text, (secret, text)
        assert {"base_url", "api_key"}.isdisjoint(set(_strings(info)))
        providers["p2"]["api_key"] = "sk-later-2"             # 加上密钥：下一次 info 就是 True
        providers["p3"]["api_key"] = "sk-later-3"
        providers["p1"].pop("api_key")                        # 删掉密钥：下一次 info 就是 False
        again = _ok(client.post(f"{API}/info", json={}))["key_set"]
        assert again == {"extraction": False, "embedding": True, "chat": False, "consolidation": True}
        assert "sk-later" not in json.dumps(_ok(client.post(f"{API}/info", json={})))


def test_info_key_set_leaves_out_unbound_roles(tmp_path):
    """没有 endpoint 的角色不出现在 key_set 里（这里只绑了抽取；chat 没绑时回退到抽取的 provider，C++ 设置里也是它）。"""
    providers = {"p1": {"provider": "openai", "model": "m1", "base_url": SECRET_URL, "api_key": SECRET_KEY}}
    app = create_app(_cfg(tmp_path, providers=providers, roles={"extraction": "p1"}),
                     engine_factory=_stub_factory, idle_close_interval_s=0)
    with TestClient(app) as client:
        info = _ok(client.post(f"{API}/info", json={}))
        bound = {role for role in ("extraction", "embedding", "chat", "consolidation")
                 if isinstance(info["default_settings"].get(role), dict)}
        assert set(info["key_set"]) == bound and "embedding" not in bound and "consolidation" not in bound
        assert all(info["key_set"].values())


def test_info_default_settings_round_trip_through_open_unchanged(svc):
    """R93 的往返（R101 钉住）：info()["default_settings"] 原样传给 open(settings=...) 回 200，可写、只读都一样；key_set 不在
    default_settings 里，所以 R35 对客户端设置里 key_set 的拒绝不受影响。"""
    client, _clock = svc
    info = _ok(client.post(f"{API}/info", json={}))
    assert "key_set" in info
    settings = info["default_settings"]
    sid = _connect(client)
    _ok(client.post(f"{API}/admin/create_world", json={"name": "rt", "settings": settings}))
    _ok(_open(client, sid, world="rt", read_only=True, settings=settings))
    _ok(_open(client, sid, world="rt", settings=settings))
    with_key_set = json.loads(json.dumps(settings))
    with_key_set["extraction"]["key_set"] = True
    _err(_open(client, _connect(client), world="rt", read_only=True, settings=with_key_set), "invalid_settings")
