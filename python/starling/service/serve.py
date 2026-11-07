"""`starling-serve`：记忆服务进程的入口（spec §10.1 启动、§10.2 停止，记忆服务第 4 期 Task 4.3）。

    starling-serve --data-dir D --host 127.0.0.1 --port P [--token-file F] [--drain-timeout S]
                   [--idle-close-interval S]

- 所有路径都在 D 之下：D/starling.json（可以没有；没有抽取 provider 时从 OPENAI_* 补一个）、D/dashboard.db（默认世界）、
  D/registry.db、D/worlds/、D/ingest-spool/。不读 ~/.starling，也不看环境变量里的路径（DashboardConfig.load_service）。
- token 必须给：--token-file F 或环境变量 STARLING_SERVICE_TOKEN；没有 --token 参数（命令行参数 ps 能看到）。从不把 token
  写回 starling.json。
- 数据目录锁：启动时对 D/.lock 取 fcntl.flock(LOCK_EX | LOCK_NB)，在绑端口之前。拿不到就在 stderr 打印 data_dir_in_use、
  退出码 3。锁跟着这个进程的文件描述符走，进程退出时由内核释放，不写 pid 文件。
- 就绪：先绑端口（stdout 打印「starling-serve listening on http://HOST:PORT」，端口 0 时是系统分配的那个），再在工作线程里
  开注册表、建默认世界的引擎；开好之前 /health 与其余 API 一律 503 worlds_unavailable（create_app 的 deferred_open），
  开好之后 stdout 打印「starling-serve ready」。开的途中出错：记日志，进程退出码 1（交给进程管理器重启，不留一个永远 503
  的服务）。
- 空闲关闭：每 --idle-close-interval 秒（默认 30，0 关掉）一轮，按 C++ 的空闲判定关掉没人用的世界（默认世界不关）。
- SIGTERM / SIGINT：停止接受新连接 → 等在途请求结束（上限 --drain-timeout，默认 30 秒）→ lifespan 的关停：各世界进
  DRAINING、停后台线程、逐个关闭世界（合并 WAL、写统计缓存）→ 释放数据目录锁 → 退出码 0。

scripts/run_dashboard.py 经 run(data_dir=~/.starling, token_from_config=True, config=...) 走同一个入口：配置仍由
DashboardConfig.load 从 ~/.starling/starling.json 读（没有 token 时照旧生成并写回），库、端口、注册表位置都与之前一样；
新增的只有上面的数据目录锁、就绪前 503 与 SIGTERM 的有界等待。

这里只做进程层面的事（参数、锁、端口、信号、退出码）；世界、引擎、会话的语义都在引擎池与 C++ 里。
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import socket
import sys
from pathlib import Path

import uvicorn

from starling.dashboard.app import create_app
from starling.dashboard.config import DashboardConfig

__all__ = ["DataDirInUse", "lock_data_dir", "main", "run"]

TOKEN_ENV = "STARLING_SERVICE_TOKEN"
LOCK_NAME = ".lock"
DEFAULT_DRAIN_TIMEOUT_S = 30.0
DEFAULT_IDLE_CLOSE_INTERVAL_S = 30.0
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DATA_DIR_IN_USE = 3
_BACKLOG = 2048


class DataDirInUse(Exception):
    """D/.lock 已被另一个进程持有（错误码 data_dir_in_use）。"""

    code = "data_dir_in_use"


def lock_data_dir(data_dir: str | os.PathLike) -> int:
    """建好数据目录，对 D/.lock 取排他、非阻塞的 flock，返回持锁的文件描述符（调用方留着它直到退出；关掉它或进程退出
    即释放）。被别的进程持有时抛 DataDirInUse。"""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise DataDirInUse(str(root)) from None
        raise
    return fd


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(_BACKLOG)
        sock.set_inheritable(False)
    except BaseException:
        sock.close()
        raise
    return sock


def _say(line: str) -> None:
    print(line, flush=True)


def run(data_dir: str | os.PathLike, *, token_from_config: bool = False,
        config: DashboardConfig | None = None, token: str | None = None,
        host: str | None = None, port: int | None = None,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        idle_close_interval_s: float = DEFAULT_IDLE_CLOSE_INTERVAL_S, engine_factory=None) -> int:
    """跑一个服务进程直到被 SIGTERM / SIGINT 停下，返回退出码（0 正常停止，1 启动失败，3 data_dir_in_use）。

    token_from_config=False（starling-serve）：配置是 DashboardConfig.load_service(data_dir, token)，token 必须给。
    token_from_config=True（run_dashboard.py）：配置是调用方给的 config（缺省 DashboardConfig.load()），token、库、注册表位置
    都按它，与之前一样。host / port 缺省取配置。"""
    if token_from_config:
        cfg = config if config is not None else DashboardConfig.load()
    else:
        if not token:
            raise ValueError("starling-serve needs a token (--token-file or STARLING_SERVICE_TOKEN)")
        cfg = DashboardConfig.load_service(str(data_dir), token)
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port
    try:
        lock_fd = lock_data_dir(data_dir)
    except DataDirInUse:
        print(f"data_dir_in_use: {data_dir} is locked by another starling-serve process",
              file=sys.stderr, flush=True)
        return EXIT_DATA_DIR_IN_USE
    try:
        return _serve(cfg, drain_timeout_s, idle_close_interval_s, engine_factory)
    finally:
        os.close(lock_fd)                     # 世界都关完了才放锁


def _serve(cfg: DashboardConfig, drain_timeout_s: float, idle_close_interval_s: float,
           engine_factory) -> int:
    try:
        sock = _bind(cfg.host, cfg.port)
    except OSError as exc:
        print(f"cannot listen on {cfg.host}:{cfg.port}: {exc}", file=sys.stderr, flush=True)
        return EXIT_FAILED
    failed: list[BaseException] = []
    server: uvicorn.Server | None = None

    def on_ready() -> None:
        _say("starling-serve ready")

    def on_open_failed(exc: BaseException) -> None:
        failed.append(exc)
        if server is not None:
            server.should_exit = True

    try:
        app = create_app(cfg, engine_factory=engine_factory, deferred_open=True,
                         on_ready=on_ready, on_open_failed=on_open_failed,
                         idle_close_interval_s=idle_close_interval_s)
        server = uvicorn.Server(uvicorn.Config(
            app, timeout_graceful_shutdown=drain_timeout_s, lifespan="on"))

        def on_signal(_signum, _frame) -> None:
            # uvicorn 在 serve 期间换上自己的处理器，退出时换回这个、再把收到的信号重发一次：这里只置标志，进程不会在关完
            # 世界之后被默认处理器以 -15 杀掉。serve 之前收到的信号同样只是让它一起来就关停。
            server.should_exit = True

        previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
        shown_host, shown_port = sock.getsockname()[:2]
        _say(f"starling-serve listening on http://{_url_host(shown_host)}:{shown_port}")
        try:
            server.run(sockets=[sock])
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    finally:
        sock.close()
    return EXIT_FAILED if failed else EXIT_OK


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _read_token(token_file: str | None) -> str | None:
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip() or None
    return (os.environ.get(TOKEN_ENV) or "").strip() or None


def _parser() -> argparse.ArgumentParser:
    # allow_abbrev=False：否则 argparse 把 --token 当成 --token-file 的前缀缩写收下（命令行上的 token，spec §10.1 不允许）。
    ap = argparse.ArgumentParser(prog="starling-serve", allow_abbrev=False,
                                 description="Starling memory service (spec §10).")
    ap.add_argument("--data-dir", required=True, help="data directory D; every path lives under it")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True, help="0 lets the OS pick a free port")
    ap.add_argument("--token-file", default=None,
                    help=f"file holding the bearer token (else the {TOKEN_ENV} environment variable)")
    ap.add_argument("--drain-timeout", type=float, default=DEFAULT_DRAIN_TIMEOUT_S,
                    help="seconds to wait for in-flight requests after SIGTERM (default 30)")
    ap.add_argument("--idle-close-interval", type=float, default=DEFAULT_IDLE_CLOSE_INTERVAL_S,
                    help="seconds between idle-close rounds; 0 disables idle close (default 30)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    try:
        token = _read_token(args.token_file)
    except OSError as exc:
        ap.error(f"cannot read --token-file: {exc.strerror}")
    if not token:
        ap.error(f"a token is required: --token-file F or the {TOKEN_ENV} environment variable")
    if args.drain_timeout < 0:
        ap.error("--drain-timeout must be >= 0")
    if args.idle_close_interval < 0:
        ap.error("--idle-close-interval must be >= 0")
    data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    return run(data_dir, token=token, host=args.host, port=args.port,
               drain_timeout_s=args.drain_timeout, idle_close_interval_s=args.idle_close_interval)


if __name__ == "__main__":
    sys.exit(main())
