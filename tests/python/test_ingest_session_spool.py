import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ingest_session",
    str(Path(__file__).resolve().parents[2] / "scripts" / "ingest_session.py"))
ingest_session = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ingest_session)


@pytest.fixture(autouse=True)
def _isolate_hook_log(tmp_path, monkeypatch):
    """LOG 默认是 ~/.starling/ingest.log：malformed stdin 等用例会往真实日志追加 "hook failed"。
    一律改指 tmp（个别用例自己再覆盖成 fake_home 下的路径也照常生效）。"""
    monkeypatch.setattr(ingest_session, "LOG", tmp_path / "hook-ingest.log")


def test_write_job_creates_spool_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    p = ingest_session.write_job("s1", "/t/x.jsonl", "/proj/foo", "default")
    assert p.exists()
    job = json.loads(p.read_text())
    assert job["session_id"] == "s1" and job["transcript_path"] == "/t/x.jsonl"
    assert job["cwd"] == "/proj/foo" and job["tenant"] == "default"
    assert p.parent.name == "tenant-default"


def test_script_runs_standalone_without_starling_package(tmp_path):
    spool = tmp_path / "spool"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}")
    script = Path(__file__).resolve().parents[2] / "scripts" / "ingest_session.py"
    env = os.environ.copy()
    env["STARLING_DASH_INGEST_SPOOL"] = str(spool)
    env["STARLING_DASH_TENANT"] = "tenant/with spaces"

    result = subprocess.run(
        [sys.executable, "-I", str(script), "--bootstrap", str(transcript)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    jobs = list((spool / "tenant-tenant%2Fwith%20spaces").glob("*.json"))
    assert len(jobs) == 1
    assert json.loads(jobs[0].read_text())["tenant"] == "tenant/with spaces"


def test_hook_main_reads_stdin_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s2", "transcript_path": "/t/y.jsonl", "cwd": "/proj/bar"})))
    monkeypatch.setattr("sys.argv", ["ingest_session.py"])
    ingest_session.main()                       # 不抛
    jobs = list((tmp_path / "spool" / "tenant-default").glob("*.json"))
    assert len(jobs) == 1 and json.loads(jobs[0].read_text())["session_id"] == "s2"


def test_malformed_stdin_exits_zero_no_throw(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    monkeypatch.setattr("sys.argv", ["ingest_session.py"])
    ingest_session.main()                       # 绝不抛(hook 不能非零退出)


def test_bootstrap_writes_job_per_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr("sys.argv",
                        ["ingest_session.py", "--bootstrap", "/t/a.jsonl", "/t/b.jsonl"])
    ingest_session.main()
    assert len(list((tmp_path / "spool" / "tenant-default").glob("*.json"))) == 2


# --- Controller resolution: transcript_path 可能缺失于 SessionEnd payload,须按
# ~/.claude/projects/<slug>/<session_id>.jsonl (slug = cwd 所有 "/" 换成 "-") 重建,
# 且只在重建路径确实存在时才用它写 job。---

def test_missing_transcript_path_reconstructs_from_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = "/proj/foo"
    slug = cwd.replace("/", "-")
    transcript_dir = fake_home / ".claude" / "projects" / slug
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "s3.jsonl"
    transcript_file.write_text("{}")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s3", "cwd": cwd})))
    monkeypatch.setattr("sys.argv", ["ingest_session.py"])
    ingest_session.main()
    jobs = list((tmp_path / "spool" / "tenant-default").glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text())
    assert job["session_id"] == "s3"
    assert job["transcript_path"] == str(transcript_file)
    assert job["cwd"] == cwd


def test_missing_transcript_and_no_file_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_session, "SPOOL", tmp_path / "spool")
    fake_home = tmp_path / "home2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(ingest_session, "LOG", fake_home / ".starling" / "ingest.log")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s4", "cwd": "/proj/bar"})))
    monkeypatch.setattr("sys.argv", ["ingest_session.py"])
    ingest_session.main()                        # 不抛
    assert not list((tmp_path / "spool").rglob("*.json"))
    assert (fake_home / ".starling" / "ingest.log").exists()   # skip 记了日志
