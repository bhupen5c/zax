"""Multi-step project DAG: dependency unblocking and synthesis."""
import json

from zax import db, project, llm


def _proj_with_chain():
    """Project: task B depends on task A (both created blocked/ready accordingly)."""
    p = db.create_project("demo goal")
    a = db.create_task("Step A", "", project_id=p["id"], status="inbox")
    b = db.create_task("Step B", "", project_id=p["id"], status="blocked")
    db.execute("UPDATE tasks SET deps=? WHERE id=?", (a["id"], b["id"]))
    return p, a, b


async def test_advance_unblocks_when_dependency_done():
    p, a, b = _proj_with_chain()
    # B stays blocked while A is unfinished.
    assert await project.advance() is False
    assert db.get_task(b["id"])["status"] == "blocked"
    # Finish A → advance unblocks B.
    db.update_task(a["id"], status="done", result="A result", score=90)
    assert await project.advance() is True
    assert db.get_task(b["id"])["status"] == "inbox"


async def test_advance_synthesizes_completed_project(monkeypatch):
    async def fake_chat(system, messages, **k):
        return ("FINAL: integrated deliverable", 10)
    monkeypatch.setattr(llm, "chat", fake_chat)
    p, a, b = _proj_with_chain()
    db.update_task(a["id"], status="done", result="A", score=90)
    db.update_task(b["id"], status="done", result="B", score=88)
    await project.advance()  # both done → synthesize
    done = db.get_project(p["id"])
    assert done["status"] == "done"
    assert "integrated deliverable" in done["result"]


async def test_failed_dependency_fails_dependent():
    p, a, b = _proj_with_chain()
    db.update_task(a["id"], status="failed", result="oops")
    await project.advance()
    assert db.get_task(b["id"])["status"] == "failed"
