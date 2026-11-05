import gc
import importlib.util
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-historical", action="store_true", default=False,
                     help="执行依赖固定历史评测产物的用例；各历史文件须在独立进程运行")


def pytest_configure(config):
    config.addinivalue_line("markers", "historical_eval(reason): 依赖固定历史数据或原生构建的回放")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-historical"):
        return
    for item in items:
        marker = item.get_closest_marker("historical_eval")
        if marker is not None:
            reason = marker.kwargs.get("reason", "固定历史评测产物")
            item.add_marker(pytest.mark.skip(reason=f"历史回放需 --run-historical：{reason}"))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown():
    """fixture 释放后回收不可达连接环，避免长套件耗尽本机 HTTP 描述符。"""
    try:
        return (yield)
    finally:
        gc.collect()


@pytest.fixture(autouse=True)
def isolate_dashboard_ingest_spool(tmp_path, monkeypatch):
    """Dashboard tests must never inspect or mutate the user's live spool
    (nor write a worlds registry into the user's real ~/.starling)."""
    monkeypatch.setenv("STARLING_DASH_INGEST_SPOOL", str(tmp_path / "ingest-spool"))
    # 注册表（registry.db）与新世界目录都在数据目录下（裁定 R5）。
    monkeypatch.setenv("STARLING_DATA_DIR", str(tmp_path / "data"))
    # DashboardConfig 没给 config_path 时 save() 落到模块级默认路径 ~/.starling/starling.json。
    # POST /api/config 之类的用例会触发 save()，默认路径一律改指 tmp，不得覆盖真实配置。
    from starling.dashboard import config as dash_config
    fake_home = tmp_path / "home-starling"
    monkeypatch.setattr(dash_config, "_DEFAULT_DIR", fake_home)
    monkeypatch.setattr(dash_config, "_DEFAULT_CONFIG", fake_home / "starling.json")
    monkeypatch.setattr(dash_config, "_DEFAULT_DB", fake_home / "dashboard.db")
    monkeypatch.setattr(dash_config, "_DEFAULT_INGEST_SPOOL", fake_home / "ingest-spool")
    monkeypatch.delenv("STARLING_CONFIG", raising=False)


@pytest.fixture
def core():
    from starling import _core
    return _core


@pytest.fixture(scope="session")
def native_core_path():
    """已安装 `starling._core` 扩展的实际文件路径（随平台/解释器后缀变化，不得硬编码）。

    本机原生行为测试在子进程里用 spec_from_file_location 载入该文件并校验 `__file__`，
    保证被测的是当前安装的扩展而非其它副本。
    """
    spec = importlib.util.find_spec("starling._core")
    if spec is None or not spec.origin or not Path(spec.origin).is_file():
        pytest.fail("starling._core 扩展未安装：先 `python scripts/configure_build.py --build --python-editable`")
    return Path(spec.origin).resolve()
