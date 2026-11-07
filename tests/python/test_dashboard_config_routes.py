import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from starling.dashboard import DashboardConfig, create_app
from starling.dashboard.engine import DashboardEngine


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfgfile = tmp_path / "starling.json"
    cfg = DashboardConfig(db_path=str(tmp_path / "c.db"), token="", config_path=str(cfgfile))
    eng = DashboardEngine(cfg)
    client = TestClient(create_app(cfg, engine=eng))
    return cfg, eng, client, cfgfile


def test_get_config_masks_keys(ctx):
    cfg, eng, client, _ = ctx
    cfg.providers["default"] = {"provider": "openai", "model": "m",
                                "base_url": "", "api_key": "sk-secret-1234"}
    cfg.roles["extraction"] = "default"
    body = client.get("/api/config").json()
    assert body["providers"]["default"]["key_set"] is True
    assert body["roles"]["extraction"] == "default"
    assert "api_key" not in body["providers"]["default"]
    assert "secret" not in json.dumps(body)


def test_post_config_upserts_binds_persists_0600_and_hot_swaps(ctx, monkeypatch):
    cfg, eng, client, cfgfile = ctx
    # avoid real network: stub the chat-adapter builder so set_llm yields a non-None obj
    from starling import _core
    import starling.dashboard.engine as engmod
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: _core.FakeLLMAdapter())
    r = client.post("/api/config", json={
        "providers": {"default": {"provider": "openai", "model": "m", "base_url": "", "api_key": "sk-x"}},
        "roles": {"extraction": "default"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["providers"]["default"]["key_set"] is True
    assert body["roles"]["extraction"] == "default"
    assert cfgfile.exists() and stat.S_IMODE(os.stat(cfgfile).st_mode) == 0o600
    assert json.loads(cfgfile.read_text())["providers"]["default"]["api_key"] == "sk-x"
    assert eng.llm is not None                       # extraction hot-swapped


def test_post_config_blank_key_keeps_stored_secret(ctx, monkeypatch):
    cfg, eng, client, cfgfile = ctx
    from starling import _core
    import starling.dashboard.engine as engmod
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: _core.FakeLLMAdapter())
    client.post("/api/config", json={
        "providers": {"default": {"provider": "openai", "model": "m1", "api_key": "sk-keep"}},
        "roles": {"extraction": "default"}})
    # edit model only, blank api_key → secret preserved
    client.post("/api/config", json={"providers": {"default": {"model": "m2", "api_key": ""}}})
    stored = json.loads(cfgfile.read_text())["providers"]["default"]
    assert stored["api_key"] == "sk-keep" and stored["model"] == "m2"


def test_bind_unknown_provider_rejected(ctx):
    cfg, eng, client, _ = ctx
    r = client.post("/api/config", json={"roles": {"extraction": "nope"}})
    assert r.status_code == 400


def test_delete_provider_unbinds_role(ctx, monkeypatch):
    cfg, eng, client, _ = ctx
    from starling import _core
    import starling.dashboard.engine as engmod
    monkeypatch.setattr(engmod, "_build_chat_adapter", lambda c: _core.FakeLLMAdapter())
    client.post("/api/config", json={
        "providers": {"default": {"provider": "openai", "model": "m", "api_key": "sk-x"}},
        "roles": {"extraction": "default"}})
    r = client.delete("/api/config/provider/default")
    assert r.status_code == 200
    body = r.json()
    assert "default" not in body["providers"]
    assert body["roles"]["extraction"] == ""          # unbound
    assert eng.llm is None                             # extraction hot-swapped off


def test_get_config_never_returns_token(ctx):
    cfg, eng, client, _ = ctx
    assert "token" not in client.get("/api/config").json()


class _RaisingWorker:
    def tick_one_batch(self, now):
        raise RuntimeError("permanent_400")   # provider rejected the embedding config


def test_gist_thresholds_persist_and_hot_swap(ctx):
    """#38-C v2 threshold surface: POST gist_thresholds → persisted, returned, and
    hot-swapped onto the engine's core (which threads them into run_idle/run_sleep)."""
    cfg, eng, client, cfgfile = ctx
    knobs = {"min_holders": 4, "min_confidence": 0.75, "similarity_threshold": 0.85,
             "entity_gist_enabled": 1}
    r = client.post("/api/config", json={"gist_thresholds": knobs})
    assert r.status_code == 200
    assert r.json()["gist_thresholds"] == knobs
    assert json.loads(cfgfile.read_text())["gist_thresholds"] == knobs
    assert eng._core.gist_thresholds == knobs  # hot-swapped (incl. v2 similarity_threshold)


def test_reembed_provider_error_is_non_fatal(ctx, monkeypatch):
    """REGRESSION: saving a provider/model whose embedding call is rejected
    (e.g. a chat model bound to the embedding role → permanent_400) must NOT 500.
    The new embedder is swapped in and the cleared rows defer to the background
    tick; _reembed logs + swallows the provider error rather than propagating it
    up through the config-save handler."""
    cfg, eng, client, _ = ctx
    monkeypatch.setattr(eng._core, "worker", _RaisingWorker())
    warn = eng._reembed()   # must return normally, not raise
    assert warn and "embedding" in warn.lower()   # surfaced as a warning, not swallowed silently


@pytest.mark.parametrize("gist", [{"min_holders": "abc"}, {"entity_gist_enabled": True}])
def test_invalid_settings_rejected_before_anything_is_written(tmp_path, monkeypatch, gist):
    """裁定 R31：C++ 校验不过的设置先校验、后写盘。回 400 invalid_settings；starling.json 逐字节不变，
    内存里的 config 不变（同一请求里一并给的 provider 与角色绑定也回滚）；用这个文件重新启动照常。"""
    import copy
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfgfile = tmp_path / "starling.json"
    cfg = DashboardConfig(db_path=str(tmp_path / "c.db"), token="", tick_interval_s=0,
                          config_path=str(cfgfile), gist_thresholds={"min_holders": 3})
    cfg.save()
    before = cfgfile.read_bytes()
    snapshot = copy.deepcopy(cfg.to_dict())
    client = TestClient(create_app(cfg))
    r = client.post("/api/config", json={
        "providers": {"p2": {"provider": "openai", "model": "m", "api_key": "sk-new"}},
        "roles": {"extraction": "p2"},
        "gist_thresholds": gist})
    assert (r.status_code, r.json()) == (400, {"code": "invalid_settings", "transient": False,
                                              "detail": "invalid_settings"})
    assert cfgfile.read_bytes() == before
    assert cfg.to_dict() == snapshot
    assert client.get("/api/config").json()["gist_thresholds"] == {"min_holders": 3}
    # 用这个文件重新启动：create_app 同步默认世界的设置不报 invalid_settings。load 在文件里没有 token
    # 时会生成一个（并写回），请求带上它。
    reloaded = DashboardConfig.load(str(cfgfile))
    with TestClient(create_app(reloaded)) as restarted:
        auth = {"Authorization": f"Bearer {reloaded.token}"}
        assert restarted.get("/api/overview", headers=auth).status_code == 200
        assert restarted.get("/api/config", headers=auth).json()["gist_thresholds"] == {
            "min_holders": 3}
