"""客户端门面经 DashboardEngine 按 holder 调（记忆服务第 4 期 Task 4.5，裁定 R76–R78、R88）。

- MemoryCore.remember_event / abandon_event 收 holder 关键字参数，缺省是 self.agent（Memory 与已有调用方不变）。
- DashboardEngine 加转发方法：remember_event / abandon_event / content_ids / digest / pending_events / next_seq / embed_pending，
  recall 加 strict / include_volatile 转发。写入类方法在只读引擎上 world_read_only。
- _core.world_settings_equal（C++ settings_equal，比存着的设置）与 _core.build_commit（构建时的 git 提交）。

离线：FakeLLMAdapter、占位嵌入，不发网络请求。
"""
import dataclasses
import re
import sqlite3

import pytest

import starling
from starling import _core
from starling.dashboard.config import DashboardConfig
from starling.dashboard.engine import DashboardEngine, WorldContext

CANNED_JSON = (
    '[{"holder":"alice","holder_perspective":"FIRST_PERSON",'
    '"subject":"cog-bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _engine(tmp_path, *, read_only=False):
    cfg = DashboardConfig(db_path=str(tmp_path / "dash.db"), token="", tick_interval_s=0.0,
                          source_lane_enabled=False, ingest_spool_path=str(tmp_path / "spool"))
    ctx = WorldContext.from_config(cfg)
    if read_only:
        DashboardEngine(ctx).close()                  # 先建好库（只读打开不建库）
        ctx = dataclasses.replace(ctx, read_only=True)
    engine = DashboardEngine(ctx)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(CANNED_JSON, True, "")
    engine.llm = fake
    return engine


# ───────────────────────── MemoryCore：holder 关键字参数 ─────────────────────────

def test_memory_facade_keeps_its_agent_as_the_event_holder(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice",
                               llm=starling.make_stub_llm(default_response=CANNED_JSON))
    mem.remember_event("k1", "bob owns auth", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        assert conn.execute("SELECT holder_id FROM events WHERE key='k1'").fetchone() == ("alice",)
    mem.close()


def test_memory_core_remember_and_abandon_event_take_a_holder(tmp_path):
    engine = _engine(tmp_path)
    core = engine._core
    core.llm = engine.llm
    core.remember_event("k1", "bob owns auth", holder="carol", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "dash.db")) as conn:
        assert conn.execute("SELECT holder_id FROM events WHERE key='k1'").fetchone() == ("carol",)
    failing = _core.FakeLLMAdapter()
    failing.set_default_response("", False, "stub extraction failure")
    core.llm = failing
    core.remember_event("k2", "pending text", holder="carol", interlocutor="bob")
    with pytest.raises(KeyError):
        core.abandon_event("k2")                      # 缺省 holder 是 agent，不是 carol
    core.abandon_event("k2", holder="carol")
    with sqlite3.connect(str(tmp_path / "dash.db")) as conn:
        assert conn.execute("SELECT state FROM events WHERE key='k2'").fetchone() == ("abandoned",)
    engine.close()


# ───────────────────────── DashboardEngine 的转发方法 ─────────────────────────

def test_engine_event_methods_forward_with_the_handle_holder(tmp_path):
    engine = _engine(tmp_path)
    assert engine.next_seq() == 0
    out = engine.remember_event("k1", "bob owns auth", holder="carol", interlocutor="bob",
                                epoch="2026-01-01T00:00:00Z")
    assert out["outcome"] == "accepted" and out["statement_ids"]
    assert engine.next_seq() == 1 and engine.pending_events() == []
    ids = engine.content_ids(out["statement_ids"] + ["nope"])
    assert ids["nope"] == "missing" and re.fullmatch(r"[0-9a-f]{64}", ids[out["statement_ids"][0]])
    assert re.fullmatch(r"[0-9a-f]{64}", engine.digest())
    stats = engine.embed_pending("2026-01-01T00:00:05Z")
    assert set(stats) == {"embedded", "failed"} and stats["embedded"] >= 1
    hits = engine.recall("bob owns auth", holder="carol", strict=True, include_volatile=True)
    assert hits and hits[0]["row"].holder_id == "carol" and "content_id" in hits[0]
    assert engine.recall("bob owns auth", holder="dave", include_volatile=True) == []
    failing = _core.FakeLLMAdapter()
    failing.set_default_response("", False, "stub extraction failure")
    engine.llm = failing
    engine.remember_event("k2", "pending text", holder="carol", interlocutor="bob")
    assert [(ev["key"], ev["holder"]) for ev in engine.pending_events()] == [("k2", "carol")]
    with pytest.raises(KeyError):
        engine.abandon_event("k2", holder="dave")
    engine.abandon_event("k2", holder="carol")
    assert engine.pending_events() == []
    with pytest.raises(_core.EventConflict):
        engine.remember_event("k2", "pending text", holder="carol", interlocutor="bob")
    engine.close()


def test_engine_recall_strict_raises_recall_degraded_when_query_embedding_fails(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    engine._core.set_embedder(starling.make_openai_embedder(
        model="offline", dim=8, timeout_ms=10, base_url="unsupported-starling-test://invalid"))
    with pytest.raises(starling.RecallDegradedError):
        engine.recall("anything", strict=True)
    assert engine.recall("anything") == []           # 看板的缺省照旧不 strict
    engine.close()


def test_engine_write_methods_on_a_read_only_engine_are_world_read_only(tmp_path):
    engine = _engine(tmp_path, read_only=True)
    for call in (lambda: engine.remember_event("k", "t", holder="carol", interlocutor="bob"),
                 lambda: engine.abandon_event("k", holder="carol"),
                 lambda: engine.embed_pending("2026-01-01T00:00:00Z")):
        with pytest.raises(_core.WorldStoreError) as err:
            call()
        assert _core.world_store_error_code(err.value) == "world_read_only"
    assert engine.next_seq() == 0 and engine.pending_events() == []
    assert re.fullmatch(r"[0-9a-f]{64}", engine.digest())
    engine.close()


# ───────────────────────── 绑定：settings_equal、build_commit ─────────────────────────

def test_world_settings_equal_forwards_to_cpp():
    assert _core.world_settings_equal({}, {}) is True
    assert _core.world_settings_equal({}, {"vector_backend": "sqlite"}) is True
    assert _core.world_settings_equal({}, {"background": {"ingest": False}}) is False
    with pytest.raises(_core.WorldStoreError) as err:
        _core.world_settings_equal({}, {"api_key": "k"})
    assert _core.world_store_error_code(err.value) == "invalid_settings"


def test_build_commit_is_unknown_or_forty_hex():
    commit = _core.build_commit()
    assert commit == "unknown" or re.fullmatch(r"[0-9a-f]{40}", commit)
