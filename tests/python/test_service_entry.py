"""starling-serve 入口（记忆服务第 4 期 Task 4.3，spec §10.1、§10.2、§16 第 7 条）。

每条用例在子进程里起服务：数据目录 D 在 tmp 下，HOME 指到 tmp，端口给 0 让系统分配，从 stdout 的
「listening」行读回端口、「ready」行判断就绪。子进程的环境去掉了 OPENAI_* 与 STARLING_*（只按用例需要再设），
所以不会读到本机的真实配置；用例结束时（含失败）一律杀掉子进程。
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t43-service-token"
_LISTENING = re.compile(r"starling-serve listening on http://127\.0\.0\.1:(\d+)")
_READY = "starling-serve ready"
_START_TIMEOUT_S = 60.0


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

def _clean_env(home: Path, **extra: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OPENAI_", "STARLING_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


class _Service:
    """一个 starling-serve 子进程。stdout 由读线程逐行放进队列；stderr 写到文件（访问日志多，管道会写满）。"""

    def __init__(self, argv: list[str], env: dict, cwd: Path, stderr_path: Path) -> None:
        self.stderr_path = stderr_path
        self._stderr = open(stderr_path, "wb")
        self.proc = subprocess.Popen(argv, env=env, cwd=str(cwd), stdout=subprocess.PIPE,
                                     stderr=self._stderr, text=True)
        self.lines: queue.Queue = queue.Queue()
        self.stdout: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.port: int | None = None

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.stdout.append(line)
            self.lines.put(line)
        self.lines.put(None)

    def wait_line(self, predicate, timeout_s: float = _START_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no matching stdout line; stdout={self.stdout!r}; "
                                     f"stderr={self.stderr()[-2000:]!r}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"service exited ({self.proc.wait()}) before the line; "
                                     f"stdout={self.stdout!r}; stderr={self.stderr()[-2000:]!r}")
            if predicate(line):
                return line

    def wait_listening(self) -> int:
        line = self.wait_line(lambda text: _LISTENING.search(text) is not None)
        self.port = int(_LISTENING.search(line).group(1))
        return self.port

    def wait_ready(self) -> None:
        self.wait_line(lambda text: _READY in text)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=30.0,
                            headers={"Authorization": f"Bearer {TOKEN}"})

    def stderr(self) -> str:
        self._stderr.flush()
        return self.stderr_path.read_text("utf-8", "replace")

    def terminate(self, timeout_s: float = 30.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout_s)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._stderr.close()


@pytest.fixture
def services(tmp_path):
    """起服务的工厂；用例结束时（含失败）杀掉每一个还活着的子进程。"""
    started: list[_Service] = []
    home = tmp_path / "home"
    home.mkdir()

    def start(data_dir: Path, *, argv_extra: tuple[str, ...] = (), env_extra: dict | None = None,
              token_file: bool = True, launcher: Path | None = None) -> _Service:
        index = len(started)
        argv = [sys.executable]
        argv += [str(launcher)] if launcher is not None else ["-m", "starling.service"]
        argv += ["--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", "0"]
        env = _clean_env(home, **(env_extra or {}))
        if token_file:
            token_path = tmp_path / f"token-{index}"
            token_path.write_text(TOKEN + "\n", encoding="utf-8")
            argv += ["--token-file", str(token_path)]
        argv += list(argv_extra)
        service = _Service(argv, env, tmp_path, tmp_path / f"stderr-{index}.log")
        started.append(service)
        return service

    start.home = home
    try:
        yield start
    finally:
        for service in started:
            service.kill()


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


