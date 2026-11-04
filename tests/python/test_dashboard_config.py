import json
import os
import stat

from starling.dashboard.config import DashboardConfig


def test_load_defaults_and_token_autogen(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLING_DASH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg_path = tmp_path / "starling.json"
    cfg = DashboardConfig.load(str(cfg_path))
    assert cfg.token and len(cfg.token) >= 20          # auto-generated
    assert cfg_path.exists()                            # persisted
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
    # token stable across reloads
    cfg2 = DashboardConfig.load(str(cfg_path))
    assert cfg2.token == cfg.token


def test_file_then_env_precedence(tmp_path, monkeypatch):
    cfg_path = tmp_path / "starling.json"
    cfg_path.write_text(json.dumps({
        "token": "filetok", "host": "127.0.0.1", "port": 9000,
        "providers": {"openai-main": {"provider": "openai", "model": "m-file",
                                      "base_url": "", "api_key": "k-file"}},
        "roles": {"extraction": "openai-main"},
    }))
    monkeypatch.setenv("STARLING_DASH_PORT", "9999")    # env overrides file
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = DashboardConfig.load(str(cfg_path))
    assert cfg.port == 9999 and cfg.token == "filetok"
    assert cfg.extraction()["api_key"] == "k-file"
    assert cfg.roles["extraction"] == "openai-main"


def test_save_roundtrip_excludes_config_path(tmp_path):
    cfg = DashboardConfig(db_path="x.db", token="t", config_path=str(tmp_path / "c.json"))
    cfg.providers["default"] = {"provider": "openai", "model": "gpt",
                                "base_url": "", "api_key": "sk-xyz"}
    cfg.roles["extraction"] = "default"
    cfg.save()
    data = json.loads((tmp_path / "c.json").read_text())
    assert "config_path" not in data
    assert data["providers"]["default"]["api_key"] == "sk-xyz"
    assert data["roles"]["extraction"] == "default"
    import os as _os, stat as _stat
    assert _stat.S_IMODE(_os.stat(tmp_path / "c.json").st_mode) == 0o600


def test_legacy_llm_embedder_migration(tmp_path, monkeypatch):
    """REGRESSION: an old {llm, embedder} file migrates to providers/roles on
    load, and extraction/embedding still resolve to the same models/keys."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg_path = tmp_path / "starling.json"
    cfg_path.write_text(json.dumps({
        "token": "t",
        "llm": {"provider": "anthropic", "model": "claude-x", "base_url": "", "api_key": "k-llm"},
        "embedder": {"provider": "openai", "model": "emb-x", "base_url": "", "api_key": "k-emb", "dim": 1536},
    }))
    cfg = DashboardConfig.load(str(cfg_path))
    ex = cfg.extraction()
    em = cfg.embedding()
    assert ex["provider"] == "anthropic" and ex["model"] == "claude-x" and ex["api_key"] == "k-llm"
    assert em["model"] == "emb-x" and em["api_key"] == "k-emb" and em["dim"] == 1536
    # migration is one-way + persisted: re-saving yields the new shape, no llm/embedder keys
    cfg.save()
    data = json.loads(cfg_path.read_text())
    assert "providers" in data and "roles" in data
    assert "llm" not in data and "embedder" not in data
    assert data["roles"]["extraction"] and data["roles"]["embedding"]


def test_env_seeds_extraction_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-env")
    cfg = DashboardConfig.load(str(tmp_path / "starling.json"))
    ex = cfg.extraction()
    assert ex is not None and ex["api_key"] == "sk-env" and ex["model"] == "gpt-env"


def test_save_without_config_path_never_reaches_the_real_home(tmp_path):
    """没给 config_path 的 DashboardConfig（world_routes 等用例的 client 夹具就是这样建的）save() 时
    落到模块级默认路径。conftest 把默认路径改指 tmp；这里钉住：写到的是 tmp 下的副本，
    不是 ~/.starling/starling.json。"""
    from pathlib import Path
    from starling.dashboard import config as dash_config
    real = Path.home() / ".starling" / "starling.json"
    assert dash_config._DEFAULT_CONFIG != real
    assert str(dash_config._DEFAULT_CONFIG).startswith(str(tmp_path))
    before = real.stat().st_mtime_ns if real.exists() else None
    cfg = DashboardConfig(db_path=str(tmp_path / "x.db"), token="t")
    cfg.save()
    assert Path(cfg.config_path) == dash_config._DEFAULT_CONFIG
    assert dash_config._DEFAULT_CONFIG.exists()
    after = real.stat().st_mtime_ns if real.exists() else None
    assert after == before
