#!/usr/bin/env python3
"""One-click launcher for the Starling dashboard.

Loads unified config (~/.starling/starling.json; token auto-generated on first
run), builds the SvelteKit frontend if missing (needs node), then serves the
FastAPI engine-API + the static frontend on a single port. Prints a login URL
with the token in the URL fragment (#token=…), which browsers do NOT send to
the server — so the token never lands in access logs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from starling.dashboard import DashboardConfig
from starling.service import serve

_WEB = Path(__file__).resolve().parents[1] / "dashboard" / "web"


def _ensure_build(no_build: bool) -> None:
    build = _WEB / "build"
    if build.is_dir() or no_build:
        return
    if not (_WEB / "package.json").exists():
        return
    if subprocess.run(["npm", "--version"], capture_output=True).returncode != 0:
        sys.exit("frontend build missing and npm not found — run `npm ci && npm run build` in dashboard/web")
    print("building frontend (first run)…", file=sys.stderr)
    subprocess.run(["npm", "ci"], cwd=str(_WEB), check=True)
    subprocess.run(["npm", "run", "build"], cwd=str(_WEB), check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()

    cfg = DashboardConfig.load(args.config)
    cfg.validate_bind()
    _ensure_build(args.no_build)
    # 与 starling-serve 走同一个入口（spec §10.1）：配置、token、库、端口照旧来自 starling.json（token 没有时 load 生成并写回），
    # 注册表在 cfg.data_dir（环境变量 STARLING_DATA_DIR，没设就是默认世界库文件所在的目录，即 ~/.starling）。入口另加：
    # 数据目录锁（D/.lock，第二个进程退出码 3）、默认世界引擎建好之前 API 回 503、SIGTERM 后在途请求最多等 30 秒。
    data_dir = cfg.data_dir or str(Path.home() / ".starling")
    shown = cfg.host if cfg.host not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"\nDashboard ready → http://{shown}:{cfg.port}/#token={cfg.token}\n", flush=True)
    sys.exit(serve.run(data_dir, token_from_config=True, config=cfg))


if __name__ == "__main__":
    main()
