"""starling-serve 入口（记忆服务第 4 期 Task 4.3，spec §10.1、§10.2、§16 第 7 条）。

每条用例在子进程里起服务：数据目录 D 在 tmp 下，HOME 指到 tmp，端口给 0 让系统分配，从 stdout 的
「listening」行读回端口、「ready」行判断就绪。子进程的环境去掉了 OPENAI_* 与 STARLING_*（只按用例需要再设），
所以不会读到本机的真实配置；用例结束时（含失败）一律杀掉子进程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from fakes.service_proc import LISTENING as _LISTENING
from fakes.service_proc import START_TIMEOUT_S as _START_TIMEOUT_S
from fakes.service_proc import ServiceFactory
from fakes.service_proc import ServiceProc as _Service
from fakes.service_proc import clean_env as _clean_env

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t43-service-token"


# ───────────────────────── 本机假抽取端点 ─────────────────────────

def _stub(obj: str) -> str:
    return json.dumps([{"holder": "self", "holder_perspective": "FIRST_PERSON", "subject": "Bob",
                        "predicate": "responsible_for", "object": obj, "modality": "BELIEVES",
                        "polarity": "POS", "nesting_depth": 0}])


class _FakeOpenAI(BaseHTTPRequestHandler):
    """OpenAI 兼容的 /chat/completions：按提示里的关键词回一条语句（三次写入得到三条不同的语句）。"""

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — http.server 的约定
        size = int(self.headers.get("Content-Length", 0))
        prompt = self.rfile.read(size).decode("utf-8", "replace")
        obj = next((word for word in ("billing", "infra", "auth") if word in prompt), "auth")
        body = json.dumps({"choices": [{"message": {"content": _stub(obj)},
                                        "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_openai():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


# ───────────────────────── 子进程服务 ─────────────────────────
# 子进程辅助（clean_env、ServiceProc、ServiceFactory）在 fakes/service_proc.py，与验收测试共用。

@pytest.fixture
def services(tmp_path):
    """起服务的工厂；用例结束时（含失败）杀掉每一个还活着的子进程。"""
    factory = ServiceFactory(tmp_path, token=TOKEN)
    try:
        yield factory
    finally:
        factory.kill_all()


def _write_config(data_dir: Path, base_url: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "starling.json").write_text(json.dumps({
        "providers": {"fake": {"provider": "openai", "model": "fake-model", "base_url": base_url,
                               "api_key": "fake-key"}},
        "roles": {"extraction": "fake"},
        "source_lane_enabled": False,
    }), encoding="utf-8")


def _statement_ids(client: httpx.Client) -> set[str]:
    resp = client.get("/api/statements")
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()["rows"]}


# ───────────────────────── 用例 ─────────────────────────

def test_second_server_same_data_dir_exits_data_dir_in_use(services, tmp_path):
    data_dir = tmp_path / "D"
    first = services(data_dir)
    first.wait_listening()
    first.wait_ready()
    second = services(data_dir)
    assert second.proc.wait(30) == 3
    assert "data_dir_in_use" in second.stderr()
    assert not any(_LISTENING.search(line) for line in second.stdout)   # 拿锁在绑端口之前
    with first.client() as client:
        assert client.get("/health").status_code == 200                  # 第一个照常服务


def test_token_not_accepted_from_argv(tmp_path):
    env = _clean_env(tmp_path, STARLING_SERVICE_TOKEN=TOKEN)
    proc = subprocess.run([sys.executable, "-m", "starling.service", "--data-dir", str(tmp_path / "D"),
                           "--port", "0", "--token", TOKEN],
                          env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2                                   # argparse 的用法错误
    assert "unrecognized arguments: --token" in proc.stderr
    assert not (tmp_path / "D").exists()                          # 什么都没建


def test_token_is_required(tmp_path):
    """--token-file 与 STARLING_SERVICE_TOKEN 都没给：拒绝启动（spec §10.1「token 必须给」），不建数据目录。"""
    proc = subprocess.run([sys.executable, "-m", "starling.service", "--data-dir", str(tmp_path / "D"),
                           "--port", "0"],
                          env=_clean_env(tmp_path), cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 2
    assert "STARLING_SERVICE_TOKEN" in proc.stderr
    assert not (tmp_path / "D").exists()


def test_token_from_environment_is_enforced(services, tmp_path):
    service = services(tmp_path / "D", token_file=False, env_extra={"STARLING_SERVICE_TOKEN": TOKEN})
    service.wait_listening()
    service.wait_ready()
    with httpx.Client(base_url=service.url, timeout=30.0) as anon:
        assert anon.get("/api/worlds").status_code == 401
    with service.client() as client:
        assert client.get("/api/worlds").status_code == 200


def test_sigterm_then_restart_keeps_committed_writes(services, tmp_path, fake_openai):
    """§16 第 7 条：写入 3 条后 SIGTERM，30 秒内退出码 0；同一数据目录重启后 3 条都在。"""
    data_dir = tmp_path / "D"
    _write_config(data_dir, fake_openai)
    service = services(data_dir)
    service.wait_listening()
    service.wait_ready()
    with service.client() as client:
        for text in ("Bob owns auth", "Bob owns billing", "Bob owns infra"):
            resp = client.post("/api/remember", json={"text": text})
            assert resp.status_code == 200, resp.text
            assert resp.json()["statement_ids"]
        before = _statement_ids(client)
    assert len(before) == 3
    started = time.monotonic()
    assert service.terminate(timeout_s=30.0) == 0
    assert time.monotonic() - started < 30.0
    assert "data_dir_in_use" not in service.stderr()

    again = services(data_dir)                     # 锁已释放：同一数据目录能再起来
    again.wait_listening()
    again.wait_ready()
    with again.client() as client:
        assert _statement_ids(client) == before


_GATED_LAUNCHER = '''
import os
import sys
import time

import starling.dashboard.app as app_module

_real_open_pool = app_module._open_pool


def _gated_open_pool(*args, **kwargs):
    # 就绪之前的窗口撑开：注册表等到闸门文件出现才打开。
    while not os.path.exists(os.environ["T43_GATE"]):
        time.sleep(0.05)
    return _real_open_pool(*args, **kwargs)


app_module._open_pool = _gated_open_pool
from starling.service import serve
sys.exit(serve.main(sys.argv[1:]))
'''


def test_health_503_until_ready(services, tmp_path):
    """spec §10.1：注册表打开、默认世界引擎建好之前，/health 与其余 API 一律 503 worlds_unavailable（瞬态）；
    之后 /health 200。用一个把注册表打开卡在闸门上的启动脚本撑开这个窗口。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "gated_serve.py"
    launcher.write_text(_GATED_LAUNCHER, encoding="utf-8")
    gate = tmp_path / "gate"
    service = services(tmp_path / "D", launcher=launcher, env_extra={"T43_GATE": str(gate)})
    service.wait_listening()
    unavailable = {"code": "worlds_unavailable", "transient": True, "detail": "worlds_unavailable"}
    with service.client() as client:
        for path in ("/health", "/api/worlds", "/api/statements", "/api/w/default/overview"):
            resp = client.get(path)
            assert (resp.status_code, resp.json()) == (503, unavailable), path
        assert client.post("/api/remember", json={"text": "x"}).status_code == 503
        assert not (tmp_path / "D" / "registry.db").exists()       # 确实还没就绪
        gate.write_text("open", encoding="utf-8")
        service.wait_ready()
        resp = client.get("/health")
        assert resp.status_code == 200 and resp.json()["status"] == "ok"
        assert client.get("/api/worlds").status_code == 200


def test_data_dir_is_the_only_root(services, tmp_path):
    """spec §10.1：一个空的 D 起服务，所有文件都落在 D 之下；~/.starling（HOME 指到 tmp）的 mtime 与内容不变。
    子进程里另设指向别处的 STARLING_DATA_DIR / STARLING_DASH_DB / STARLING_CONFIG / STARLING_DASH_INGEST_SPOOL，
    服务一律不用。"""
    home_starling = services.home / ".starling"
    home_starling.mkdir()
    before = (home_starling.stat().st_mtime_ns, sorted(os.listdir(home_starling)))
    elsewhere = tmp_path / "elsewhere"
    data_dir = tmp_path / "D"
    data_dir.mkdir()
    service = services(data_dir, env_extra={
        "STARLING_DATA_DIR": str(elsewhere / "data"),
        "STARLING_DASH_DB": str(elsewhere / "x.db"),
        "STARLING_CONFIG": str(elsewhere / "c.json"),
        "STARLING_DASH_INGEST_SPOOL": str(elsewhere / "spool"),
    })
    service.wait_listening()
    service.wait_ready()
    with service.client() as client:
        assert client.get("/api/worlds").status_code == 200
        assert client.post("/api/worlds", json={"name": "w1"}).status_code == 200
        assert client.get("/api/w/w1/overview").status_code == 200
    assert service.terminate() == 0
    assert (home_starling.stat().st_mtime_ns, sorted(os.listdir(home_starling))) == before
    assert not elsewhere.exists()
    names = set(os.listdir(data_dir))
    assert {"registry.db", "dashboard.db", "worlds", ".lock"} <= names, names
    assert "starling.json" not in names                           # 没有配置文件时不写一个出来（不落 token）


def test_starling_serve_console_script_is_declared():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        scripts = tomllib.load(fh)["project"].get("scripts", {})
    assert scripts.get("starling-serve") == "starling.service.serve:main"


# ───────────────────────── 修复轮：R83 M3、R85 ─────────────────────────

def test_websocket_before_ready_is_accepted_then_closed_1013(services, tmp_path):
    """R83 M3：就绪之前的 WebSocket 先 accept、再以 1013（Try Again Later）关闭：客户端拿到明确的关闭码，而不是握手被拒
    （HTTP 403）。"""
    import asyncio

    import websockets
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "gated_serve.py"
    launcher.write_text(_GATED_LAUNCHER, encoding="utf-8")
    gate = tmp_path / "gate"
    service = services(tmp_path / "D", launcher=launcher, env_extra={"T43_GATE": str(gate)})
    service.wait_listening()

    async def _probe():
        url = f"ws://127.0.0.1:{service.port}/ws/converse?token={TOKEN}"
        async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            try:
                await asyncio.wait_for(ws.recv(), 10)
            except websockets.ConnectionClosed as closed:
                return closed.rcvd.code if closed.rcvd else None
        return None
    try:
        assert asyncio.run(_probe()) == 1013
    finally:
        gate.write_text("open", encoding="utf-8")
    service.wait_ready()




_DASHBOARD_PROBE = '''
import json
import os
import socket
import sys
import time

import starling.dashboard.app as app_module

_real_open_pool = app_module._open_pool
_port = int(os.environ["T43_PORT"])


def _probing_open_pool(*args, **kwargs):
    # token_from_config（run_dashboard / 8787）的路径：池开好之前端口上不能有监听（R85）。在开池时探一次。
    sock = socket.socket()
    try:
        listening = sock.connect_ex(("127.0.0.1", _port)) == 0
    finally:
        sock.close()
    print(json.dumps({"listening_while_opening": listening}), flush=True)
    time.sleep(0.5)
    return _real_open_pool(*args, **kwargs)


app_module._open_pool = _probing_open_pool
from starling.dashboard import DashboardConfig
from starling.service import serve
cfg = DashboardConfig.load(os.environ["T43_CONFIG"])
sys.exit(serve.run(os.environ["T43_DATA"], token_from_config=True, config=cfg))
'''


def test_token_from_config_path_opens_the_pool_before_listening(tmp_path):
    """R85：run_dashboard / 8787 的路径（token_from_config=True）维持改动之前的顺序：先开池、再监听。开池时端口上没有监听，
    之后 /health 一来就是 200（watcher 的 curl -sf 不会碰到 503）。"""
    import socket as _socket
    probe_sock = _socket.socket()
    probe_sock.bind(("127.0.0.1", 0))
    port = probe_sock.getsockname()[1]
    probe_sock.close()
    data_dir = tmp_path / "home" / ".starling"
    data_dir.mkdir(parents=True)
    config = data_dir / "starling.json"
    config.write_text(json.dumps({"db_path": str(data_dir / "dashboard.db"), "port": port,
                                  "token": TOKEN, "tick_interval_s": 0,
                                  "source_lane_enabled": False}), encoding="utf-8")
    script = tmp_path / "probe_dashboard.py"
    script.write_text(_DASHBOARD_PROBE, encoding="utf-8")
    env = _clean_env(tmp_path / "home", T43_PORT=str(port), T43_CONFIG=str(config),
                     T43_DATA=str(data_dir))
    service = _Service([sys.executable, str(script)], env, tmp_path, tmp_path / "stderr-probe.log")
    try:
        line = service.wait_line(lambda text: "listening_while_opening" in text)
        assert json.loads(line) == {"listening_while_opening": False}
        deadline = time.monotonic() + _START_TIMEOUT_S
        first = None
        while time.monotonic() < deadline:
            try:
                first = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
                break
            except httpx.TransportError:
                time.sleep(0.05)
        assert first is not None and first.status_code == 200      # 第一次连得上就是 200，没有 503
        assert service.terminate() == 0
    finally:
        service.kill()
