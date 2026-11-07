"""Build hook: write starling_client/_build_info.py (COMMIT, VERSION)."""
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_build_info(version: str) -> Path:
    path = HERE / "starling_client" / "_build_info.py"
    path.write_text(f'COMMIT = "{_commit()}"\nVERSION = "{version}"\n')
    return path
