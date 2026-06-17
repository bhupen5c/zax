"""Unit tests for the execution pipeline (zax/pipeline.py)."""
import time

import pytest

from zax import ceo, db, learning, llm, pipeline


@pytest.fixture(autouse=True)
def reset_pipeline_state():
    pipeline._clear_backoff()
    pipeline._draining = False
    pipeline._pending = False
    yield
    pipeline._clear_backoff()


# ---------------------------------------------------------------- happy path

async def test_run_executes_and_reviews(seeded):
    t = db.create_task("Write a short summary", "brief")
    res = await pipeline.run(5, force=True)
    assert res["ok"] is True and res["executed"] >= 1
    cur = db.get_task(t["id"])
    assert cur["status"] in ("done", "failed") and cur["score"] is not None


# ---------------------------------------------------------------- backoff

def test_exponential_backoff_grows():
    pipeline._clear_backoff()
    pipeline._trip_backoff("e1"); w1 = pipeline._backoff_until - time.time()
    pipeline._trip_backoff("e2"); w2 = pipeline._backoff_until - time.time()
    pipeline._trip_backoff("e3"); w3 = pipeline._backoff_until - time.time()
    assert round(w1) <= 31 and 55 <= round(w2) <= 61 and 115 <= round(w3) <= 121


def test_backoff_caps_at_max():
    pipeline._clear_backoff()
    for _ in range(20):
        pipeline._trip_backoff("e")
    assert pipeline._backoff_until - time.time() <= pipeline.BACKOFF_MAX + 1


def test_clear_backoff_resets():
    pipeline._trip_backoff("e")
    pipeline._clear_backoff()
    assert pipeline._backoff_until == 0 and pipeline._consecutive_failures == 0


async def test_run_skips_during_backoff_but_force_clears(seeded):
    pipeline._trip_backoff("core down")
    db.create_task("X")
    res = await pipeline.run(5)  # not forced
    assert res["ok"] is False and res.get("skipped") == "backoff"
    res2 = await pipeline.run(5, force=True)  # force clears
    assert res2["ok"] is True


# ---------------------------------------------------------------- phase isolation

async def test_reflection_failure_does_not_block_execution(seeded, monkeypatch):
    async def boom():
        raise RuntimeError("reflection exploded")
    monkeypatch.setattr(learning, "maybe_daily_reflection", boom)
    t = db.create_task("Do the real work")
    res = await pipeline.run(5, force=True)
    # execution proceeded despite the reflection crash
    assert res["ok"] is True
    assert db.get_task(t["id"])["status"] in ("done", "failed")


async def test_execute_failure_trips_backoff_but_review_still_runs(seeded, monkeypatch):
    # One task already executed and awaiting review:
    a = db.active_agents()[0]
    reviewed_me = db.create_task("already executed")
    db.update_task(reviewed_me["id"], status="in_progress", agent_id=a["id"],
                   result="a finished deliverable")
    # A fresh task whose execution will fail:
    fail_me = db.create_task("will fail")

    calls = {"n": 0}
    real_chat = llm.chat

    async def flaky(system, messages, **k):
        # fail only agent execution, allow review (performance-review prompt)
        if "performance review" in system.lower():
            return await real_chat(system, messages, **k)
        calls["n"] += 1
        raise RuntimeError("execution provider down")
    monkeypatch.setattr(llm, "chat", flaky)

    res = await pipeline.run(5, force=True)
    assert res["ok"] is False  # execution failed -> breaker tripped
    # but the previously-completed task STILL got reviewed
    assert db.get_task(reviewed_me["id"])["score"] is not None


# ---------------------------------------------------------------- run_now

async def test_run_now_reports_pending(seeded):
    db.create_task("a")
    db.create_task("b")
    res = await pipeline.run_now()
    assert "pending_before" in res and isinstance(res["pending_before"], int)
