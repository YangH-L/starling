"""Build hook: write starling_client/_build_info.py (COMMIT, VERSION).

VERSION comes from starling_client/_version.py (the only place it is written).
"""
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_version() -> str:
    ns: dict = {}
    exec((HERE / "starling_client" / "_version.py").read_text(), ns)
    return ns["__version__"]


def _commit() -> str:
    # Diagnostic field only. A copy vendored inside another git repo would report
    # that repo's HEAD; that is not a supported install path.
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_build_info() -> Path:
    path = HERE / "starling_client" / "_build_info.py"
    path.write_text(f'COMMIT = "{_commit()}"\nVERSION = "{read_version()}"\n')
    return path
