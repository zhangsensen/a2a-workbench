from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from a2a.types import TaskState
from fastapi.testclient import TestClient

import roundtable
from roundtable import create_app
from test_execute_channel import configure
from test_roundtable import FakeMember


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], check=True,
    )
    (path / "protected.txt").write_text("frozen\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "protected.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)
    return path


def test_execute_adds_structured_delivery_and_evidence(tmp_path, monkeypatch):
    configure(monkeypatch, {"codex": "http://127.0.0.1:10002"})
    repo = make_repo(tmp_path / "repo")

    async def fake_call(url, prompt, room, cwd, on_task_id):
        await on_task_id("remote-verified")
        (Path(cwd) / "result.txt").write_text("ready\n", encoding="utf-8")
        return TaskState.TASK_STATE_COMPLETED, "built", .2

    monkeypatch.setattr(roundtable, "call_executor", fake_call)
    check = {
        "type": "command",
        "argv": [
            sys.executable, "-c",
            "from pathlib import Path; assert Path('result.txt').read_text() == 'ready\\n'",
        ],
    }
    with TestClient(create_app(tmp_path / "data", FakeMember)) as client:
        response = client.post("/api/rooms/lobby/execute", json={
            "executor": "codex",
            "text": "Build result",
            "requestId": "verified-core",
            "cwd": str(repo),
            "mode": "modify",
            "verify": [check],
            "protected": ["protected.txt"],
        })
        assert response.status_code == 200, response.text
        result = client.get(
            "/api/jobs/verified-core?room=lobby&waitSeconds=5"
        ).json()

    assert result["state"] == "completed"
    metadata = json.loads(result["events"][-1]["metadata"])
    assert metadata["delivery"]["changed_files"] == ["result.txt"]
    assert Path(metadata["delivery"]["patch"]).is_file()
    evidence = metadata["evidence"]
    assert evidence["verdict"] == "verified"
    assert evidence["contract"] == {
        "task": "Build result",
        "mode": "modify",
        "verify": [check],
        "protected": ["protected.txt"],
    }
    assert len(evidence["contract_digest"]) == 64
    assert evidence["candidate"][0]["passed"] is True
    assert evidence["protected"][0]["changed"] is False


def test_protected_change_short_circuits_rooms_checks(tmp_path, monkeypatch):
    configure(monkeypatch, {"codex": "http://127.0.0.1:10002"})
    repo = make_repo(tmp_path / "repo")

    async def fake_call(url, prompt, room, cwd, on_task_id):
        (Path(cwd) / "protected.txt").write_text("tampered\n", encoding="utf-8")
        return TaskState.TASK_STATE_COMPLETED, "done", .1

    monkeypatch.setattr(roundtable, "call_executor", fake_call)
    with TestClient(create_app(tmp_path / "data", FakeMember)) as client:
        response = client.post("/api/rooms/lobby/execute", json={
            "executor": "codex",
            "text": "Do not touch protected",
            "requestId": "protected-core",
            "cwd": str(repo),
            "verify": [{"type": "command", "argv": [sys.executable, "-c", "raise SystemExit(9)"]}],
            "protected": ["protected.txt"],
        })
        assert response.status_code == 200, response.text
        result = client.get(
            "/api/jobs/protected-core?room=lobby&waitSeconds=5"
        ).json()

    evidence = json.loads(result["events"][-1]["metadata"])["evidence"]
    assert result["state"] == "completed"
    assert evidence["verdict"] == "contract-changed"
    assert evidence["candidate"] == []
    assert evidence["protected"][0]["changed"] is True


def test_verify_requires_cwd_and_rejects_escaping_paths(tmp_path, monkeypatch):
    configure(monkeypatch, {"codex": "http://127.0.0.1:10002"})
    with TestClient(create_app(tmp_path / "data", FakeMember)) as client:
        missing_cwd = client.post("/api/rooms/lobby/execute", json={
            "executor": "codex", "text": "Check", "requestId": "missing-cwd",
            "verify": [{"type": "command", "argv": [sys.executable, "-V"]}],
        })
        escaping = client.post("/api/rooms/lobby/execute", json={
            "executor": "codex", "text": "Check", "requestId": "escaping",
            "cwd": str(tmp_path),
            "verify": [{"type": "file", "path": "../outside"}],
        })
    assert missing_cwd.status_code == 422
    assert escaping.status_code == 422
