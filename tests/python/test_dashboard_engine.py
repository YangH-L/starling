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
    for call in (lambda: eng2.recall("auth"), lambda: eng2.tick("2026-11-04T00:00:00Z"),
                 lambda: eng2.working_set("Alice")):
        with pytest.raises(_core.WorldStoreError) as ei:
            call()
        assert _core.world_store_error_code(ei.value) == "endpoint_changed"


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
