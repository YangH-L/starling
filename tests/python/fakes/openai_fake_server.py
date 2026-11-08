"""本机回环的 OpenAI 兼容假端点（记忆服务第 4 期 Task 4.6，spec §16「假端点」）。

参照 rise 的 tests/starling_fakes.py 写的独立一份（不导入 rise）。服务端跑在**子进程**里：服务子进程与进程内 Memory
都经 C++ 的 curl 发请求，假端点不能跟测试进程抢 GIL；计数与开关经同一端口上的 /__fake__/ 控制路径同步。父进程退出
（或关掉 stdin）时子进程读到 EOF，立即 os._exit。只监听 127.0.0.1，不发任何外网请求。

- POST .../embeddings：input 逐条按词面哈希成确定向量（fake_vector）。fail_embeddings 打开时回 503。
- POST .../chat/completions：按提示词开头判断是哪一遍抽取。信念那一遍把「[key] 说话方：原话」抽成一条语句；
  episodic、general fact 两遍回 "[]"。
  - fail_keys：信念那一遍回 500（抽取失败，事件留在 pending）。
  - malformed_keys：general fact 那一遍回 200 加一段不是 JSON 数组的内容。
  - chat_delay_s：每个抽取请求先睡这么久再答（GIL 与 close 超时用例用）。
  - hold_keys：段落 key 在其中的抽取请求挂住，直到 POST /__fake__/release（或挂满 _HOLD_CAP_S 自己放行），
    让用例确定地造出「这个写入正在途中」的窗口。
- 鉴权：Authorization 必须是 "Bearer <api_key>"（启动时给的随机 key），否则 401 并计 auth_failures。
- 计数（GET /__fake__/state）：chat_calls、embedding_calls、auth_failures，chat_in_flight（此刻正在处理、还没答
  的抽取请求数，GIL 用例据此判断抽取已经进入阻塞），held（此刻被 hold_keys 挂住的请求数）。

测试进程里用 FakeOpenAI 句柄：start() / stop()（或 with），configure(...) 改开关，state() 取计数，wait_until(...) 有上限地
轮询计数。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# python/starling/extractor/prompts.py 里信念抽取提示词的开头
BELIEF_PROMPT_HEAD = "You are an extractor for a Statement-based memory system."
# python/starling/extractor/general_fact_prompt.py 里 general fact 抽取提示词的开头
GENERAL_FACT_PROMPT_HEAD = "You are a general-fact extractor for a Statement-based memory system."
# C++ 把段落拼成 "[" + key + "] " + text（src/memory/memory_events.cpp），text 形如「说话方：原话」
_SEGMENT = re.compile(r"\[([^\]\n]+)\] ([^：\n]+)：([^\n]+)")
_CONTROL = "/__fake__/"
_KEY_SETS = ("fail_keys", "malformed_keys", "hold_keys")
_DEFAULTS = {"fail_embeddings": False, "fail_keys": [], "malformed_keys": [], "hold_keys": [],
             "chat_delay_s": 0.0}
# hold_keys 挂住一个请求的上限（秒）：用例忘了 release 也不会把服务一直卡着。
_HOLD_CAP_S = 30.0
_MALFORMED_ANSWER = "not json"
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_KEY_ENV = "STARLING_FAKE_OPENAI_KEY"
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 5.0


def tokens(text: str) -> frozenset[str]:
    """词面集合：ASCII 词按小写整词；连续汉字按相邻二字切分，单个汉字保留单字。"""
    out = {word.lower() for word in _ASCII_WORD.findall(text)}
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return frozenset(out)


def fake_vector(text: str, *, dim: int) -> list[float]:
    """词面哈希到 dim 个桶、各加 1、再归一化；空词面给全零向量。没有共有词面（且不撞桶）时余弦恰为 0。"""
    vec = [0.0] * dim
    for tok in tokens(text):
        bucket = int(hashlib.sha256(tok.encode()).hexdigest()[:8], 16) % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return vec if norm == 0.0 else [x / norm for x in vec]


def belief_answer(speaker: str, utterance: str) -> str:
    return json.dumps([{
        "holder": "self", "holder_perspective": "FIRST_PERSON",
        "subject": f"cog-{speaker}", "predicate": "responsible_for",
        "object": utterance, "modality": "BELIEVES", "polarity": "POS", "nesting_depth": 0,
    }], ensure_ascii=False)


# ───────────────────────── 子进程里的服务端 ─────────────────────────

def _serve(dim: int, api_key: str) -> None:
    lock = threading.Lock()
    state = {"chat_calls": 0, "embedding_calls": 0, "auth_failures": 0, "chat_in_flight": 0,
             "held": 0}
    released = threading.Event()
    cfg = {name: (frozenset(value) if name in _KEY_SETS else value)
           for name, value in _DEFAULTS.items()}

    def bump(name: str, delta: int = 1) -> None:
        with lock:
            state[name] += delta

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"          # curl 复用连接（keep-alive）

        def log_message(self, *_args) -> None:
            pass

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                                   # 客户端已超时断开

        def do_GET(self) -> None:  # noqa: N802 — http.server 的约定
            if self.path == _CONTROL + "state":
                with lock:
                    self._reply(200, dict(state))
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == _CONTROL + "config":
                with lock:
                    for name, value in payload.items():
                        if name not in _DEFAULTS:
                            self._reply(400, {"error": {"message": f"unknown switch {name}"}})
                            return
                        cfg[name] = frozenset(value) if name in _KEY_SETS else value
                    if payload.get("hold_keys"):
                        released.clear()
                self._reply(200, {})
                return
            if path == _CONTROL + "release":
                released.set()
                self._reply(200, {})
                return
            if self.headers.get("Authorization") != f"Bearer {api_key}":
                bump("auth_failures")
                self._reply(401, {"error": {"message": "invalid api key"}})
                return
            if path.endswith("/embeddings"):
                self._embeddings(payload)
            elif path.endswith("/chat/completions"):
                bump("chat_calls")
                bump("chat_in_flight")
                try:
                    self._chat(payload)
                finally:
                    bump("chat_in_flight", -1)
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def _embeddings(self, payload: dict) -> None:
            bump("embedding_calls")
            with lock:
                fail = cfg["fail_embeddings"]
            if fail:
                self._reply(503, {"error": {"message": "embeddings unavailable"}})
                return
            raw = payload.get("input", [])
            items = [raw] if isinstance(raw, str) else list(raw)
            data = [{"object": "embedding", "index": i, "embedding": fake_vector(str(t), dim=dim)}
                    for i, t in enumerate(items)]
            self._reply(200, {"object": "list", "data": data, "model": payload.get("model", "fake-embed"),
                              "usage": {"prompt_tokens": 0, "total_tokens": 0}})

        def _chat(self, payload: dict) -> None:
            with lock:
                fail_keys, malformed, delay = cfg["fail_keys"], cfg["malformed_keys"], cfg["chat_delay_s"]
                hold_keys = cfg["hold_keys"]
            users = [m for m in payload.get("messages", []) if m.get("role") == "user"]
            content = users[0].get("content", "") if users else ""
            if delay:
                time.sleep(float(delay))
            answer = "[]"
            found = _SEGMENT.search(content) if isinstance(content, str) else None
            if found and found.group(1) in hold_keys:
                bump("held")
                try:
                    released.wait(_HOLD_CAP_S)
                finally:
                    bump("held", -1)
            if found and content.startswith(BELIEF_PROMPT_HEAD):
                key, speaker, utterance = found.groups()
                if key in fail_keys:
                    self._reply(500, {"error": {"message": "extraction failed"}})
                    return
                answer = belief_answer(speaker, utterance)
            elif found and content.startswith(GENERAL_FACT_PROMPT_HEAD) and found.group(1) in malformed:
                answer = _MALFORMED_ANSWER
            self._reply(200, {
                "id": "chatcmpl-fake", "object": "chat.completion",
                "model": payload.get("model", "fake-extract"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return
            traceback.print_exc(file=sys.stderr)        # 假端点自己的 bug：留在子进程 stderr，不吞
            sys.stderr.flush()

    server = Server(("127.0.0.1", 0), Handler)

    def exit_when_parent_goes_away() -> None:
        sys.stdin.read()                                # 父进程退出或关掉管道时读到 EOF
        os._exit(0)

    threading.Thread(target=exit_when_parent_goes_away, daemon=True).start()
    sys.stdout.write(f"PORT {server.server_port}\n")
    sys.stdout.flush()
    server.serve_forever()


# ───────────────────────── 测试进程里的句柄 ─────────────────────────

class FakeOpenAI:
    """with FakeOpenAI(api_key=..., dim=..., log_path=...) as fake: ...

    configure(fail_keys=[...], malformed_keys=[...], fail_embeddings=bool, chat_delay_s=float) 只改给出的开关；
    reset() 全部恢复缺省。state() 现取子进程里的计数。"""

    def __init__(self, *, api_key: str, dim: int, log_path: Path) -> None:
        self.api_key = api_key
        self.dim = dim
        self.log_path = Path(log_path)
        self._proc: subprocess.Popen | None = None
        self._log = None
        self.port = 0

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.address}/v1"

    # -- 控制路径 --
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
            if resp.status != 200:
                raise RuntimeError(f"fake control {path} -> {resp.status} {data}")
            return data
        finally:
            conn.close()

    def configure(self, **switches) -> None:
        unknown = set(switches) - set(_DEFAULTS)
        if unknown:
            raise ValueError(f"unknown switches: {sorted(unknown)}")
        body = {name: (sorted(value) if name in _KEY_SETS else value)
                for name, value in switches.items()}
        self._request("POST", _CONTROL + "config", body)

    def reset(self) -> None:
        self.configure(**_DEFAULTS)
        self.release()

    def release(self) -> None:
        """放行被 hold_keys 挂住的请求（之后再来的同 key 请求也不再挂，直到下一次 configure(hold_keys=...)）。"""
        self._request("POST", _CONTROL + "release", {})

    def state(self) -> dict:
        return self._request("GET", _CONTROL + "state")

    def wait_until(self, predicate, *, timeout_s: float = 10.0, interval_s: float = 0.005) -> dict:
        """轮询 state() 直到 predicate(state) 为真，返回那一次的 state；timeout_s 内没等到抛 TimeoutError。"""
        deadline = time.monotonic() + timeout_s
        while True:
            current = self.state()
            if predicate(current):
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError(f"fake endpoint state never matched within {timeout_s}s: {current}")
            time.sleep(interval_s)

    # -- 生命周期 --
    def start(self) -> "FakeOpenAI":
        if self._proc is not None:
            return self
        env = {key: value for key, value in os.environ.items() if not key.startswith("OPENAI_")}
        env[_KEY_ENV] = self.api_key                     # 不放命令行（ps 看得到）
        self._log = open(self.log_path, "wb")
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--serve",
                                 str(self.dim)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                env=env, text=True)
        box: list[str] = []
        reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(_START_TIMEOUT_S)
        line = box[0] if box else ""
        if not line.startswith("PORT "):
            self._shutdown(proc)
            raise RuntimeError(f"fake endpoint did not start (first line {line!r}); "
                               f"stderr: {self.log_path.read_text('utf-8', 'replace')[-2000:]}")
        self.port = int(line.split()[1])
        self._proc = proc
        return self

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            self._shutdown(proc)

    def _shutdown(self, proc: subprocess.Popen) -> None:
        """关 stdin 让子进程读到 EOF 自己退；_STOP_TIMEOUT_S 内不退就杀掉，最后一定 wait。"""
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(_STOP_TIMEOUT_S)
        if proc.stdout:
            proc.stdout.close()
        if self._log is not None:
            self._log.close()
            self._log = None

    def stderr(self) -> str:
        return self.log_path.read_text("utf-8", "replace") if self.log_path.exists() else ""

    def __enter__(self) -> "FakeOpenAI":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        _serve(int(sys.argv[2]), os.environ[_KEY_ENV])
    else:
        sys.exit("usage: openai_fake_server.py --serve DIM")
