"""Unit tests for agent task execution (zax/agents.py)."""
import json

import pytest

from zax import agents, db, llm


def _agent():
    return db.create_agent("Worker", "Generalist", "general work", "You work.", "")


async def test_execute_success_writes_result():
    a = _agent()
    t = db.create_task("Summarize X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    await agents.execute_task(a, db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert cur["result"] and cur["progress"] == 90
    assert cur["status"] == "in_progress"  # awaiting review


async def test_execute_empty_deliverable_requeues(monkeypatch):
    async def empty_chat(system, messages, **k):
        return ("   ", 0)
    monkeypatch.setattr(llm, "chat", empty_chat)
    a = _agent()
    t = db.create_task("X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    with pytest.raises(Exception):
        await agents.execute_task(a, db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert cur["status"] == "assigned" and not cur["result"]  # requeued, not saved


async def test_execute_tool_loop(monkeypatch):
    """Agent calls a tool, then delivers a final answer."""
    calls = {"n": 0}

    async def tool_then_final(system, messages, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return (json.dumps({"tool": "remember", "args": {"note": "a fact"}}), 5)
        if calls["n"] == 2:
            return (json.dumps({"final": "Done after using a tool."}), 5)
        return (json.dumps({"verdict": "pass"}), 5)  # self-check approves
    monkeypatch.setattr(llm, "chat", tool_then_final)
    a = _agent()
    t = db.create_task("Use a tool then answer")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    await agents.execute_task(a, db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert "Done after using a tool" in cur["result"]
    assert calls["n"] == 3  # one tool call + one final + one self-check
    # the remembered fact made it into company memory
    assert any("a fact" in m["content"] for m in db.list_memories())


async def test_self_check_revises_flagged_deliverable(monkeypatch):
    """The verify-before-deliver loop: checker flags a defect, ONE revision pass
    fixes it, and the finding is stored as an agent-scoped lesson."""
    calls = {"n": 0}

    async def draft_check_revise(system, messages, **k):
        calls["n"] += 1
        if calls["n"] == 1:   # agent's draft
            return (json.dumps({"final": "Draft with a defect."}), 5)
        if calls["n"] == 2:   # checker flags it
            return (json.dumps({"verdict": "fix", "issues": ["missing the requested table"]}), 5)
        if calls["n"] == 3:   # revision
            return (json.dumps({"final": "Revised deliverable with the table."}), 5)
        return (json.dumps({"verdict": "pass"}), 5)  # re-check passes → loop exits
    monkeypatch.setattr(llm, "chat", draft_check_revise)
    a = _agent()
    t = db.create_task("Compare 3 options in a table")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    await agents.execute_task(a, db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert "Revised deliverable" in cur["result"]
    assert calls["n"] == 4  # draft + check(fix) + revise + re-check(pass) — the loop
    # the defect became an agent-scoped lesson for future tasks
    assert any("missing the requested table" in m["content"] for m in db.list_memories())


async def test_tool_budget_exhaustion_forces_synthesis(monkeypatch):
    """An agent that keeps calling tools must still deliver — the forced no-tools
    synthesis turns gathered work into a real answer instead of hitting the limit."""
    from zax import config

    async def always_tool_until_told(system, messages, **k):
        last = messages[-1]["content"] if messages else ""
        # the forced-synthesis prompt says "do NOT request any more tools"
        if "do not request any more tools" in last.lower():
            return (json.dumps({"final": "Synthesized brief from gathered research."}), 5)
        return (json.dumps({"tool": "web_search", "args": {"query": "x"}}), 5)
    monkeypatch.setattr(llm, "chat", always_tool_until_told)
    a = _agent()
    t = db.create_task("Research something deep")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    await agents.execute_task(a, db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert "Synthesized brief" in cur["result"]
    assert "hit the limit" not in cur["result"].lower()
    assert "tool-step limit" not in cur["result"].lower()


def test_deliverable_unwraps_final_json():
    # clean wrapper
    assert agents._deliverable('{"final": "the answer"}') == "the answer"
    # multi-line (unescaped newlines) — wrapper must not leak
    out = agents._deliverable('{"final": "# Title\\n\\nbody text"}')
    assert out.startswith("# Title") and "final" not in out.split("\n")[0]
    # truncated wrapper (cut off by token cap) still recovers the text
    out2 = agents._deliverable('{"final": "a long answer that got cut o')
    assert "final" not in out2 and "long answer" in out2
    # plain prose passes through untouched
    assert agents._deliverable("just prose") == "just prose"


async def test_delivered_result_has_no_json_wrapper(monkeypatch):
    async def wrapped(system, messages, **k):
        return ('{"final": "## Brief\\n\\nObsidian is the pick."}', 10)
    monkeypatch.setattr(llm, "chat", wrapped)
    a = _agent()
    t = db.create_task("X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    await agents.execute_task(a, db.get_task(t["id"]))
    res = db.get_task(t["id"])["result"]
    assert res.startswith("## Brief") and '{"final"' not in res


async def test_execute_failure_requeues_and_raises(monkeypatch):
    async def boom(system, messages, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm, "chat", boom)
    a = _agent()
    t = db.create_task("X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    with pytest.raises(Exception):
        await agents.execute_task(a, db.get_task(t["id"]))
    assert db.get_task(t["id"])["status"] == "assigned"  # requeued for retry
