import os

import pytest

from starling import _core
from starling.dashboard.config import DashboardConfig
from starling.dashboard.engine import DashboardEngine, WorldContext, _LLMNotConfigured

_STUB_JSON = (
    '[{"holder":"self","holder_perspective":"FIRST_PERSON",'
    '"subject":"Bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _ctx(tmp_path, settings=None, **over) -> WorldContext:
    """一个世界的上下文：设置由 C++ 补齐默认值（与 WorldPool.context 同一条路）。"""
    base = dict(name="w1", dir_id="abc123", db_path=str(tmp_path / "w1.db"), tenant="w1",
                agent="self",
                settings=_core.world_effective_settings(
                    {"source_lane": {"enabled": False}, **(settings or {})}),
                ingest_spool_dir=str(tmp_path / "spool" / "world-abc123"), is_default=False)
    base.update(over)
    return WorldContext(**base)


def _fake():
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB_JSON, True, "")
    return fake


@pytest.fixture
def engine(tmp_path):
    cfg = DashboardConfig(db_path=str(tmp_path / "eng.db"), token="")
    eng = DashboardEngine(cfg)   # llm unset, embedder -> stub8
    return eng


def test_unconfigured_llm_recall_tick_ok_but_remember_raises(engine):
    assert isinstance(engine.recall("auth"), list)
    st = engine.tick("2026-06-01T10:00:00Z")
    assert set(st) == {"embedded", "fired", "broken", "auto_withdrawn",
                       "replay_sampled", "consolidated", "ttl_archived",
                       "projected", "dispatched", "stage_timings_ms",
                       "stages_skipped"}
    with pytest.raises(_LLMNotConfigured):
        engine.remember("Bob owns auth")


def test_set_llm_enables_remember_offline_stub(engine):
    engine.llm = _fake()
    r = engine.remember("Bob owns auth")
    assert r["outcome"] in ("accepted", "idempotent")
    st = engine.tick("2026-06-01T10:00:00Z")
    assert "embedded" in st


def test_working_set_renders(engine):
    ws = engine.working_set("Alice")
    assert "render" in ws and "blocks" in ws and "truncated" in ws


def test_working_set_includes_affect_section(engine):
    """Parity regression: the engine working set must carry the affect section
    (it drifted away from Memory.render_working_set before the MemoryCore
    consolidation)."""
    import sqlite3
    engine.llm = _fake()
    engine.remember("Bob owns auth")
    # Stamp affect + consolidate the extracted statement (engine connection
    # idle): vector recall only surfaces consolidated/archived rows, and fresh
    # extractions are volatile.
    conn = sqlite3.connect(engine._db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("UPDATE statements SET affect_json='{\"valence\":0.8,\"arousal\":0.6}',"
                 " consolidation_state='consolidated'")
    conn.commit()
    conn.close()
    engine.tick("2026-06-10T10:00:00Z")          # embed via stub embedder
    ws = engine.working_set("Alice", goal="auth")
    labels = [b["label"] for b in ws["blocks"]]
    assert "relevant_memories" in labels
    assert "affect" in labels, "engine working set lost the affect section"


# ───────────────────────── 构造：DashboardEngine(ctx, providers)（Task 5） ─────────────────────────

def test_config_adapter_builds_the_default_world_context(tmp_path):
    """裁定 R17：DashboardEngine(config) 经 WorldContext.from_config 适配；分区目录与今天一致。"""
    cfg = DashboardConfig(db_path=str(tmp_path / "eng.db"), token="", tenant="a b",
                          ingest_spool_path=str(tmp_path / "root"), source_lane_enabled=False)
    eng = DashboardEngine(cfg)
    assert eng._ctx.is_default and eng.world == "default" and eng.tenant == "a b"
    assert eng._ingest_spool == tmp_path / "root" / "tenant-a%20b"
    assert eng._providers is cfg.providers                 # 同一个 dict：改 provider 立刻可见
    assert eng._settings["source_lane"]["enabled"] is False


def test_engine_reads_the_world_context_not_a_config(tmp_path):
    eng = DashboardEngine(_ctx(tmp_path, {"background": {"tick_interval_s": 7.0}}), {})
    assert eng._cfg is None
    assert eng._db_path == str(tmp_path / "w1.db")
    assert eng._core.tenant == "w1" and eng.tenant == "w1" and eng.world == "w1"
    assert eng._ingest_spool == tmp_path / "spool" / "world-abc123"
    assert eng.llm is None                                  # 设置里没有抽取端点（裁定 R15）
    assert eng.source_lane_status()["configured"] is False
    with pytest.raises(_LLMNotConfigured):
        eng.remember("Bob owns auth")
    from starling.dashboard.engine import ClearConfirmMismatch
    with pytest.raises(ClearConfirmMismatch):
        eng.clear_memory(confirm="default")                 # 确认的是这个世界的租户
    assert eng.clear_memory(confirm="w1")["tenant"] == "w1"


def test_engine_rejects_empty_tenant_or_spool_dir(tmp_path):
    with pytest.raises(ValueError):
        DashboardEngine(_ctx(tmp_path, tenant=""), {})
    with pytest.raises(ValueError):
        DashboardEngine(_ctx(tmp_path, ingest_spool_dir=""), {})


def test_keys_come_from_providers_by_name_never_from_settings(tmp_path, monkeypatch):
    import starling.dashboard.engine as engmod
    built = []
    monkeypatch.setattr(engmod, "_build_chat_adapter",
                        lambda cfg: built.append(dict(cfg)) or _core.FakeLLMAdapter())
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m"}
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://a",
                       "api_key": "sk-from-registry"}}
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep}), providers)
    assert eng.llm is not None
    assert built[0]["api_key"] == "sk-from-registry" and built[0]["model"] == "m"
    assert "api_key" not in eng._settings["extraction"]


def test_explicit_endpoint_uses_the_service_env_key(tmp_path, monkeypatch):
    """客户端显式给的端点（provider_name 为空）取服务进程的 OPENAI_API_KEY（spec §8.3）。"""
    import starling.dashboard.engine as engmod
    built = []
    monkeypatch.setattr(engmod, "_build_chat_adapter",
                        lambda cfg: built.append(dict(cfg)) or _core.FakeLLMAdapter())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    ep = {"provider": "openai", "base_url": "http://x", "model": "m"}
    DashboardEngine(_ctx(tmp_path, {"extraction": ep}), {})
    assert built[0]["api_key"] == "sk-env"
    monkeypatch.delenv("OPENAI_API_KEY")
    built.clear()
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep}, db_path=str(tmp_path / "w2.db")), {})
    assert built == [] and eng.llm is None                 # 没有密钥：不建适配器


def test_inherited_endpoint_change_refuses_every_call_that_uses_it(tmp_path, monkeypatch):
    import starling.dashboard.engine as engmod
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://a",
                       "api_key": "sk-secret"}}
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m"}
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep}), providers)
    eng.llm = _fake()
    assert eng.remember("Bob owns auth")["outcome"] in ("accepted", "idempotent")
    providers["p"]["base_url"] = "http://b"
    for call in (lambda: eng.remember("Bob owns auth 2"), lambda: eng.converse("hi")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _core.world_store_error_code(ei.value) == "endpoint_changed"
        assert "sk-secret" not in str(ei.value)
    # 本轮选了别的 provider：不走这个世界的继承端点，不受影响。
    providers["q"] = {"provider": "openai", "model": "m", "base_url": "http://q", "api_key": "k"}
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda cfg: _fake())
    assert eng.remember("Bob owns auth 3", provider="q")["outcome"] in ("accepted", "idempotent")
    # 嵌入端点同理：召回、tick、工作集都拒绝。真嵌入适配器换成离线占位（不发网络请求），密钥照样给，
    # 所以引擎按「会发请求」对待它。
    monkeypatch.setattr(engmod, "_build_embed_adapter", lambda cfg: _core.StubEmbeddingAdapter(8))
    emb_providers = {"p": {"provider": "openai", "model": "e", "base_url": "http://a",
                           "api_key": "sk-emb"}}
    emb = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "e",
           "dim": 8}
    eng2 = DashboardEngine(_ctx(tmp_path, {"embedding": emb}, db_path=str(tmp_path / "e.db")),
                           emb_providers)
    assert isinstance(eng2.recall("auth"), list)
    emb_providers["p"]["base_url"] = "http://b"
    for call in (lambda: eng2.recall("auth"), lambda: eng2.working_set("Alice")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _core.world_store_error_code(ei.value) == "endpoint_changed"
    # tick 不抛，只跳过嵌入阶段（裁定 R54），其余阶段照常。
    stats = eng2.tick("2026-11-04T00:00:00Z")
    assert stats["stages_skipped"] == ["embed"] and stats["embedded"] == 0
    assert eng2.embedding_refused is True


def test_endpoint_change_is_not_checked_when_no_adapter_would_send_a_key(tmp_path):
    """没有密钥就没有适配器（抽取为 None、嵌入是占位），也就没有密钥会被发到新地址：照常报
    llm_not_configured、照常召回，不报 endpoint_changed。"""
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://a"}}   # 没有 api_key
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m"}
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep, "embedding": {**ep, "dim": 8}}),
                          providers)
    providers["p"]["base_url"] = "http://b"
    assert eng.llm is None
    with pytest.raises(_LLMNotConfigured):
        eng.remember("Bob owns auth")
    assert isinstance(eng.recall("auth"), list)
    eng.tick("2026-11-04T00:00:00Z")
    # 测试注入的适配器（eng.llm = fake）同样算「会用到这个端点」：核对照常进行。
    eng.llm = _fake()
    with pytest.raises(_core.WorldStoreError) as ei:
        eng.remember("Bob owns auth")
    assert _core.world_store_error_code(ei.value) == "endpoint_changed"


def test_removed_provider_counts_as_changed(tmp_path):
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://a"}}
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m"}
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep}), providers)
    eng.llm = _fake()
    providers.clear()
    with pytest.raises(_core.WorldStoreError) as ei:
        eng.remember("Bob owns auth")
    assert _core.world_store_error_code(ei.value) == "endpoint_changed"


def test_on_commit_fires_once_per_successful_write(tmp_path):
    eng = DashboardEngine(_ctx(tmp_path), {})
    eng.llm = _fake()
    calls = []
    eng.on_commit = lambda: calls.append(1)
    out = eng.remember("Bob owns auth")
    assert calls == [1]
    eng.tick("2026-11-04T00:00:00Z")                        # 嵌入了一条：做了事
    assert len(calls) == 2
    eng.tick("2026-11-04T00:00:01Z")                        # 什么都没做：不通知
    assert len(calls) == 2
    eng.forget(out["statement_ids"])
    assert len(calls) == 3
    eng.clear_memory(confirm="w1")
    assert len(calls) == 4

    def boom():
        raise RuntimeError("boom")

    eng.on_commit = boom                                    # 回调坏了，写照样成功
    assert eng.remember("Carol owns billing")["outcome"] in ("accepted", "idempotent")


def test_failed_construction_does_not_pin_the_runtime(tmp_path, monkeypatch):
    """构造失败（Runtime 已打开之后某一步抛错）时，异常回溯不能钉住那个 Runtime。"""
    import gc
    import weakref
    import starling.dashboard.engine as engmod
    built = []
    real = engmod._runtime._build_local_store_sqlite_runtime

    def spy(path):
        rt = real(path)
        built.append(weakref.ref(rt))
        return rt

    def boom(*_args, **_kwargs):
        raise RuntimeError("lane boom")

    monkeypatch.setattr(engmod._runtime, "_build_local_store_sqlite_runtime", spy)
    monkeypatch.setattr(engmod.MemoryCore, "enable_source_lane", boom)
    kept = None
    try:
        DashboardEngine(_ctx(tmp_path, {"source_lane": {"enabled": True}}), {})
    except RuntimeError as exc:
        kept = exc
    assert kept is not None and "lane boom" in str(kept)
    gc.collect()
    assert built and built[0]() is None
    assert os.path.exists(tmp_path / "w1.db")


# ───────────────────────── 修复轮 1 ─────────────────────────

def test_explicit_endpoint_never_captures_a_key_mid_env_swap(tmp_path, monkeypatch):
    """裁定 R22：别的线程正在 _env_swap 里给 provider P 建适配器时，环境变量暂时是 P 的密钥。显式端点
    的世界（provider_name 为空、地址由客户端给）读服务级密钥时不能读到它，否则 P 的密钥会被发到客户端
    给的地址（显式端点不做 endpoint_changed 核对）。"""
    import threading
    import starling.dashboard.engine as engmod
    secret = "sk-provider-p-SECRET-7f3a"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured = []
    monkeypatch.setattr(engmod, "_build_chat_adapter",
                        lambda cfg: captured.append(cfg.get("api_key")) or _core.FakeLLMAdapter())
    ep = {"provider": "openai", "base_url": "http://client-chosen", "model": "m"}
    stop = threading.Event()

    def other_provider():
        while not stop.is_set():
            with engmod._env_swap(secret, "http://p"):
                pass

    swapper = threading.Thread(target=other_provider)
    swapper.start()
    try:
        for idx in range(200):
            DashboardEngine(_ctx(tmp_path, {"extraction": ep}, db_path=str(tmp_path / f"w{idx}.db")),
                            {})
    finally:
        stop.set()
        swapper.join(5)
    assert secret not in captured
    assert all(key is None for key in captured)              # 服务进程本身没设密钥


def test_service_key_is_read_under_the_env_swap_lock(tmp_path, monkeypatch):
    import starling.dashboard.engine as engmod
    monkeypatch.setenv("OPENAI_API_KEY", "sk-service")
    assert engmod._service_openai_key() == "sk-service"
    with engmod._env_swap("sk-other", ""):
        pass
    assert engmod._service_openai_key() == "sk-service"     # 换回之后照旧


def test_consolidation_endpoint_comes_from_world_settings(tmp_path, monkeypatch):
    """裁定 R25：consolidation 是世界设置里的第四个 endpoint，按 ctx.settings 建，同样核对地址。"""
    import starling.dashboard.engine as engmod
    built = []
    monkeypatch.setattr(engmod, "_build_chat_adapter",
                        lambda cfg: built.append(dict(cfg)) or _core.FakeLLMAdapter())
    providers = {"j": {"provider": "openai", "model": "judge", "base_url": "http://j",
                       "api_key": "sk-judge"}}
    ep = {"provider": "openai", "provider_name": "j", "base_url": "http://j", "model": "judge"}
    eng = DashboardEngine(_ctx(tmp_path, {"consolidation": ep}), providers)
    assert eng._core.consolidation_llm is not None
    assert [b["model"] for b in built] == ["judge"] and built[0]["api_key"] == "sk-judge"
    assert eng.run_replay("idle")["mode"] == "idle"
    providers["j"]["base_url"] = "http://elsewhere"
    with pytest.raises(_core.WorldStoreError) as ei:
        eng.run_replay("idle")
    assert _core.world_store_error_code(ei.value) == "endpoint_changed"


def _spy_vector_index(monkeypatch):
    """这个构建没有 zvec：记下 MemoryCore 拿到的后端与路径，用 sqlite 句柄代替。"""
    import starling._memory_core as mc
    seen = []

    def spy(backend, dim, store_path):
        seen.append((backend, store_path))
        return _core.SqliteBlobVectorIndex()

    monkeypatch.setattr(mc, "_make_vector_index", spy)
    return seen


def test_config_adapter_passes_vector_backend_and_path_straight_through(tmp_path, monkeypatch):
    """裁定 R26：DashboardEngine(config) 的向量后端与路径直接取 config，不经世界设置（世界设置只收 sqlite）。"""
    seen = _spy_vector_index(monkeypatch)
    cfg = DashboardConfig(db_path=str(tmp_path / "z.db"), token="", vector_backend="zvec",
                          vector_store_path=str(tmp_path / "vectors"), source_lane_enabled=False)
    eng = DashboardEngine(cfg)
    assert seen == [("zvec", str(tmp_path / "vectors"))]
    assert eng._settings["vector_backend"] == "sqlite"       # 设置里永远是 sqlite


def test_adapter_rebuild_and_reembed_refuse_a_changed_inherited_endpoint(tmp_path, monkeypatch):
    """裁定 R38：从设置重建适配器（reload_adapters / apply_world_settings）先核对继承来的地址；重嵌入在删任何
    向量之前核对。地址改了就抛 endpoint_changed：适配器不换，statement_vectors 一行不删。"""
    import sqlite3
    import starling.dashboard.engine as engmod
    built = []

    def _embed(cfg):
        built.append(dict(cfg))
        return _core.StubEmbeddingAdapter(8)
    monkeypatch.setattr(engmod, "_build_embed_adapter", _embed)
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda cfg: _fake())
    providers = {"p": {"provider": "openai", "model": "e", "base_url": "http://a",
                       "api_key": "sk-secret"}}
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "e", "dim": 8}
    db = str(tmp_path / "w1.db")
    eng = DashboardEngine(_ctx(tmp_path, {"embedding": ep, "extraction": ep}), providers)
    eng.llm = _fake()
    eng.remember("Bob owns auth")
    eng.tick("2026-11-04T00:00:00Z")
    con = sqlite3.connect(db)
    vectors = con.execute("SELECT COUNT(*) FROM statement_vectors").fetchone()[0]
    con.close()
    assert vectors >= 1
    redirected = _core.world_effective_settings(
        {"source_lane": {"enabled": False}, "extraction": ep,
         "embedding": {**ep, "base_url": "http://evil"}})
    llm_before, built_before, settings_before = eng.llm, len(built), eng._settings
    with pytest.raises(_core.WorldStoreError) as ei:
        eng.apply_world_settings(redirected)
    assert _core.world_store_error_code(ei.value) == "endpoint_changed"
    assert eng._settings is settings_before                     # 拒绝时换回旧设置
    # 设置已经指着新地址（例如别的调用方直接换了引用）：重建与重嵌入同样拒绝。
    eng._settings = redirected
    for call in (lambda: eng.reload_adapters({"embedding"}), eng._reembed):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _core.world_store_error_code(ei.value) == "endpoint_changed"
        assert "sk-secret" not in str(ei.value)
    del ei
    eng._settings = settings_before
    assert len(built) == built_before and eng.llm is llm_before
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM statement_vectors").fetchone()[0] == vectors
    con.close()
    eng.close()


@pytest.mark.parametrize("role", ["extraction", "chat", "consolidation"])
def test_role_switched_from_unbound_to_a_keyed_endpoint_is_built_on_next_use(tmp_path, monkeypatch,
                                                                            role):
    """设置把一个角色从没绑定换成带密钥的 endpoint，却没重建适配器（例如只换了设置引用）。下一次用到它时，建时
    记下的 endpoint（无）与设置里的不同：先核对、过了就建，照常往下走，不报 llm_not_configured。"""
    import starling.dashboard.engine as engmod
    built = []

    def _chat(cfg):
        built.append(cfg.get("base_url"))
        return _fake()
    monkeypatch.setattr(engmod, "_build_chat_adapter", _chat)
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://a", "api_key": "sk"}}
    eng = DashboardEngine(_ctx(tmp_path), providers)
    assert eng.llm is None and built == []
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://a", "model": "m"}
    keyed = _core.world_effective_settings({"source_lane": {"enabled": False}, role: ep})
    eng.set_world_settings(keyed)                     # 只换设置引用，不重建适配器
    if role == "extraction":
        assert eng.remember("Bob owns auth")["outcome"] in ("accepted", "idempotent")
    elif role == "chat":
        eng.llm = _fake()                             # 回合末的抽取另给一个替身
        assert "reply" in eng.converse("hi")
    else:
        assert eng.run_replay("idle") is not None
        assert eng._core.consolidation_llm is not None
    assert built and built[0] == "http://a"
    # 设置里的地址已不是 provider 的：同样先核对，失败就 endpoint_changed、不建。
    built.clear()
    moved = {"p": {**providers["p"], "base_url": "http://b"}}
    eng2 = DashboardEngine(_ctx(tmp_path, db_path=str(tmp_path / "w2.db")), moved)
    eng2.set_world_settings(keyed)
    call = {"extraction": lambda: eng2.remember("Bob owns auth"),
            "chat": lambda: eng2.converse("hi"),
            "consolidation": lambda: eng2.run_replay("idle")}[role]
    with pytest.raises(_core.WorldStoreError) as ei:
        call()
    assert _core.world_store_error_code(ei.value) == "endpoint_changed"
    assert built == []
    eng.close()
    eng2.close()


# ───────────────────────── 裁定 R54：嵌入端点被拒只跳过嵌入阶段；日志按状态变化记一次、不带地址 ─────────────────────────

def _seed_due_commitment(db: str, tenant: str) -> None:
    """建表（一次性 runtime，引擎打开之前释放），再直插一个 ACTIVE 承诺与一个已到期的 time 触发器。"""
    import sqlite3
    from pathlib import Path
    from starling import runtime as rt
    runtime = rt._build_local_store_sqlite_runtime(Path(db))
    runtime.start()
    del runtime
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO commitments(tenant_id, stmt_id, state, broken_count, deadline, "
                     "created_at, updated_at) VALUES(?,?,?,0,?,?,?)",
                     (tenant, "c1", "ACTIVE", "2099-01-01T00:00:00Z",
                      "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"))
        conn.execute("INSERT INTO commitment_triggers(id, commitment_stmt_id, tenant_id, kind, "
                     "spec_json, status, created_at) VALUES('t1','c1',?,'time',?,'armed',?)",
                     (tenant, '{"at":"2026-11-02T00:00:00Z"}', "2026-11-01T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


def _stale_embedding_engine(tmp_path, monkeypatch):
    import starling.dashboard.engine as engmod
    monkeypatch.setattr(engmod, "_build_embed_adapter", lambda cfg: _core.StubEmbeddingAdapter(8))
    providers = {"p": {"provider": "openai", "model": "e", "base_url": "http://old.example",
                       "api_key": "sk-emb-secret"}}
    emb = {"provider": "openai", "provider_name": "p", "base_url": "http://old.example",
           "model": "e", "dim": 8}
    db = str(tmp_path / "w1.db")
    _seed_due_commitment(db, "w1")
    eng = DashboardEngine(_ctx(tmp_path, {"embedding": emb}), providers)
    providers["p"]["base_url"] = "http://new.example"          # p 挪了；w1 记的还是旧地址
    return eng, providers


def test_stale_embedding_endpoint_still_fires_commitments(tmp_path, monkeypatch):
    eng, _providers = _stale_embedding_engine(tmp_path, monkeypatch)
    try:
        stats = eng.tick("2026-11-04T00:00:00Z")
        assert stats["fired"] == 1                              # 承诺照常触发
        assert stats["stages_skipped"] == ["embed"]
        assert "embed" not in {t["stage"] for t in stats["stage_timings_ms"]}
        assert {"policy", "outbox"} <= {t["stage"] for t in stats["stage_timings_ms"]}
    finally:
        eng.close()


def test_background_loop_logs_an_endpoint_refusal_once_per_state_change(tmp_path, monkeypatch,
                                                                         caplog):
    """后台循环跑若干轮：变为被拒时记一次 warning，恢复时记一次 info，中间不逐轮记；日志里没有地址与密钥。"""
    import logging
    import threading
    eng, providers = _stale_embedding_engine(tmp_path, monkeypatch)
    rounds = threading.Semaphore(0)
    real_tick = eng.tick

    def counting_tick(now):
        try:
            return real_tick(now)
        finally:
            rounds.release()
    eng.tick = counting_tick
    caplog.set_level(logging.INFO, logger="starling.dashboard.engine")
    try:
        eng.start_background_tick(0.02)
        for _ in range(5):
            assert rounds.acquire(timeout=10)
        providers["p"]["base_url"] = "http://old.example"     # 地址改回来：恢复
        for _ in range(6):                                     # 至少一轮看到恢复后的状态
            assert rounds.acquire(timeout=10)
        eng.stop_background_tick()
    finally:
        eng.close()
    records = [r for r in caplog.records if r.name == "starling.dashboard.engine"
               and "embedding endpoint" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING, logging.INFO], \
        [(r.levelname, r.getMessage()) for r in records]
    text = caplog.text
    assert "example" not in text and "sk-emb-secret" not in text
    assert "background tick failed" not in text


def test_ingest_dead_letter_for_endpoint_changed_writes_only_the_code(tmp_path, monkeypatch):
    import json
    providers = {"p": {"provider": "openai", "model": "m", "base_url": "http://old.example",
                       "api_key": "sk-secret"}}
    ep = {"provider": "openai", "provider_name": "p", "base_url": "http://old.example", "model": "m"}
    eng = DashboardEngine(_ctx(tmp_path, {"extraction": ep}), providers)
    try:
        eng.llm = _fake()
        eng._ingest_max_attempts = 1
        providers["p"]["base_url"] = "http://new.example"
        spool = tmp_path / "spool" / "world-abc123"
        spool.mkdir(parents=True, exist_ok=True)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps(
            {"type": "user", "message": {"role": "user", "content": "Bob owns auth"}}))
        (spool / "s1.json").write_text(json.dumps(
            {"session_id": "s1", "transcript_path": str(transcript), "cwd": "/proj/foo",
             "tenant": "w1"}))
        assert eng._ingest_drain_once() == "done"
        error = (spool / "failed" / "s1.json.error").read_text()
        assert error == "endpoint_changed"
    finally:
        eng.close()


def test_explicit_endpoint_without_the_service_key_refuses_embedding_and_tick_skips_it(tmp_path, monkeypatch):
    """裁定 R100 I1：显式嵌入端点、服务环境里没有 OPENAI_API_KEY：召回与 embed_pending 抛 endpoint_key_missing（不拿占位向量），
    tick 不抛、只跳过嵌入阶段（与 endpoint_changed 同一条路，裁定 R54），embedding_refused 记下。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    emb = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "e", "dim": 8}
    eng = DashboardEngine(_ctx(tmp_path, {"embedding": emb}), {})
    try:
        for call in (lambda: eng.recall("auth"), lambda: eng.embed_pending("2026-11-04T00:00:00Z")):
            with pytest.raises(_core.WorldStoreError) as ei:
                call()
            assert _core.world_store_error_code(ei.value) == "endpoint_key_missing"
        stats = eng.tick("2026-11-04T00:00:00Z")
        assert stats["stages_skipped"] == ["embed"] and eng.embedding_refused is True
        monkeypatch.setenv("OPENAI_API_KEY", "sk-now-set")
        monkeypatch.setattr("starling.dashboard.engine._build_embed_adapter",
                            lambda cfg: _core.StubEmbeddingAdapter(8))
        assert isinstance(eng.recall("auth"), list)          # 设上密钥之后按它重建，照常召回
    finally:
        eng.close()


def test_embed_adapter_takes_timeout_and_max_retries_from_the_endpoint(monkeypatch):
    """最后修复波 B4（裁定 R98 F3）：嵌入适配器与抽取一样按 endpoint 的 timeout_ms、max_retries 建（max_retries 按 is None 判，0
    表示不重试）；没给时保持 C++ 默认。此前这两项被忽略，服务一侧的嵌入总是 60 s / 3 次重试。"""
    import starling.dashboard.engine as engmod
    seen = []

    class _Spy:
        def __init__(self, cfg):
            seen.append((cfg.timeout_ms, cfg.max_retries))
    monkeypatch.setattr(engmod._core, "OpenAIEmbeddingAdapter", _Spy)
    base = {"api_key": "k", "base_url": "http://127.0.0.1:9/v1", "model": "e", "dim": 8}
    engmod._build_embed_adapter({**base, "timeout_ms": 1234, "max_retries": 0})
    engmod._build_embed_adapter(base)
    default = _core.OpenAIEmbeddingConfig()
    assert seen == [(1234, 0), (default.timeout_ms, default.max_retries)]
