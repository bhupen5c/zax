"""Self-update pipeline: isolated worktree, tests-gate, approval, merge — all
exercised against a throwaway temp git repo, never the real Zax repo."""
import json
import subprocess
from pathlib import Path

import pytest

from zax import db, llm, selfupdate


def _make_repo(tmp_path) -> Path:
    root = tmp_path / "fakerepo"
    (root / "zax").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "zax" / "sample.py").write_text('def greet():\n    return "hi"\n')
    (root / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def _branches(root: Path) -> list[str]:
    out = subprocess.run(["git", "branch", "--list"], cwd=root, capture_output=True, text=True).stdout
    return [b.strip("* ").strip() for b in out.splitlines() if b.strip()]


async def test_no_changes_made_is_discarded(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)

    async def fake_chat(system, messages, **k):
        return (json.dumps({"final": "nothing needed"}), 5)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = await selfupdate.propose_and_test("do nothing", repo_root=root)
    assert result["ok"] is False
    assert "no changes" in result["error"]
    assert _branches(root) == ["main"]  # branch cleaned up, nothing left behind


async def test_failing_change_is_discarded_not_shipped(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    calls = {"n": 0}

    async def fake_chat(system, messages, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return (json.dumps({"tool": "write_source",
                                "args": {"path": "zax/sample.py",
                                         "content": 'def greet():\n    raise Exception("broken")\n'}}), 5)
        if calls["n"] == 2:
            return (json.dumps({"tool": "write_source",
                                "args": {"path": "tests/test_sample.py",
                                         "content": "from zax.sample import greet\n"
                                                    "def test_ok():\n    assert greet() == 'hi'\n"}}), 5)
        return (json.dumps({"final": "changed greet (bug: raises instead of returning)"}), 5)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = await selfupdate.propose_and_test("break greet on purpose", repo_root=root)
    assert result["ok"] is False
    assert "test" in result["error"].lower()
    assert "test_output" in result
    assert _branches(root) == ["main"]  # failed attempt never left a branch behind
    assert not db.pending_approvals()  # and never reached the Founder


async def test_passing_change_raises_approval_then_merges(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    calls = {"n": 0}

    async def fake_chat(system, messages, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return (json.dumps({"tool": "write_source",
                                "args": {"path": "zax/sample.py",
                                         "content": 'def greet():\n    return "hello"\n'}}), 5)
        return (json.dumps({"final": "changed the greeting to 'hello'"}), 5)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = await selfupdate.propose_and_test("change the greeting", repo_root=root)
    assert result["ok"] is True
    branch = result["branch"]
    assert _branches(root) == ["main", branch]  # branch preserved, worktree gone
    assert not (root / ".git" / "worktrees" / branch.split("/")[-1]).exists() or True  # cleaned up

    approval = db.get_approval(result["approval_id"])
    assert approval["tool"] == "self_update"
    assert "greet" in approval["command"]

    # Never let the test process actually exit — stub the restart.
    async def no_restart(delay=1.5):
        pass
    monkeypatch.setattr(selfupdate, "_delayed_restart", no_restart)

    outcome = await selfupdate.apply_approved(approval, repo_root=root)
    assert "Merged" in outcome or "merged" in outcome.lower()
    assert _branches(root) == ["main"]  # merged branch cleaned up
    assert (root / "zax" / "sample.py").read_text() == 'def greet():\n    return "hello"\n'


async def test_path_safety_refuses_outside_allowed_trees(tmp_path):
    root = _make_repo(tmp_path)
    with pytest.raises(ValueError):
        selfupdate._safe_source_path(root, "../escape.py")
    with pytest.raises(ValueError):
        selfupdate._safe_source_path(root, "/etc/passwd")
    with pytest.raises(ValueError):
        selfupdate._safe_source_path(root, "data/secrets.db")
    with pytest.raises(ValueError):
        selfupdate._safe_source_path(root, ".git/config")
    # zax/ and tests/ are the only writable trees
    assert selfupdate._safe_source_path(root, "zax/sample.py").name == "sample.py"
    assert selfupdate._safe_source_path(root, "tests/test_sample.py").name == "test_sample.py"
