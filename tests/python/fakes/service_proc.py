"""在子进程里起 starling-serve 的辅助代码（记忆服务第 4 期 Task 4.3 起用，4.6 抽到这里共用）。

- clean_env：去掉 OPENAI_* 与 STARLING_*，HOME 指到给定目录，所以子进程读不到本机的真实配置与密钥。
- ServiceProc：一个服务子进程。stdout 由读线程逐行放进队列（从「listening」行读回端口、「ready」行判断就绪）；
  stderr 写到文件（访问日志多，管道会写满）。所有等待都有上限。
- ServiceFactory：起服务的工厂，记下起过的每个子进程；kill_all() 由夹具在 finally 里调，用例失败也会清掉。
"""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

LISTENING = re.compile(r"starling-serve listening on http://127\.0\.0\.1:(\d+)")
READY = "starling-serve ready"
START_TIMEOUT_S = 60.0


def clean_env(home: Path, **extra: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OPENAI_", "STARLING_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


class ServiceProc:
    """一个 starling-serve 子进程。stdout 由读线程逐行放进队列；stderr 写到文件（访问日志多，管道会写满）。"""

    def __init__(self, argv: list[str], env: dict, cwd: Path, stderr_path: Path, *,
                 token: str = "") -> None:
        self.stderr_path = stderr_path
        self.token = token
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

    def wait_line(self, predicate, timeout_s: float = START_TIMEOUT_S) -> str:
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
        line = self.wait_line(lambda text: LISTENING.search(text) is not None)
        self.port = int(LISTENING.search(line).group(1))
        return self.port

    def wait_ready(self) -> None:
        self.wait_line(lambda text: READY in text)

    def start_and_wait(self) -> "ServiceProc":
        """等到 listening 与 ready 两行，返回自己。"""
        self.wait_listening()
        self.wait_ready()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> httpx.Client:
        # 测试自己的直连工具：不经这台机器的系统代理（被测的 starling_client 自己对回环地址不走代理，裁定 R97）。
        return httpx.Client(base_url=self.url, timeout=30.0, trust_env=False,
                            headers={"Authorization": f"Bearer {self.token}"})

    def stderr(self) -> str:
        if not self._stderr.closed:
            self._stderr.flush()
        return self.stderr_path.read_text("utf-8", "replace")

    def terminate(self, timeout_s: float = 30.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout_s)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._reader.join(10)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self._stderr.close()


class ServiceFactory:
    """起服务的工厂：每次调用起一个 starling-serve 子进程（端口 0，数据目录与 token 文件都在 tmp_dir 下）。
    kill_all() 杀掉每一个还活着的子进程；夹具在 finally 里调它。"""

    def __init__(self, tmp_dir: Path, *, token: str) -> None:
        self.tmp_dir = tmp_dir
        self.token = token
        self.home = tmp_dir / "home"
        self.home.mkdir(exist_ok=True)
        self.started: list[ServiceProc] = []

    def __call__(self, data_dir: Path, *, argv_extra: tuple[str, ...] = (),
                 env_extra: dict | None = None, token_file: bool = True,
                 launcher: Path | None = None) -> ServiceProc:
        index = len(self.started)
        argv = [sys.executable]
        argv += [str(launcher)] if launcher is not None else ["-m", "starling.service"]
        argv += ["--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", "0"]
        env = clean_env(self.home, **(env_extra or {}))
        if token_file:
            token_path = self.tmp_dir / f"token-{index}"
            token_path.write_text(self.token + "\n", encoding="utf-8")
            argv += ["--token-file", str(token_path)]
        argv += list(argv_extra)
        service = ServiceProc(argv, env, self.tmp_dir, self.tmp_dir / f"stderr-{index}.log",
                              token=self.token)
        self.started.append(service)
        return service

    def kill_all(self) -> None:
        for service in self.started:
            service.kill()
