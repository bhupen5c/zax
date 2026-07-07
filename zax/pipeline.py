"""The execution pipeline — assign → execute → review → HR.

This is the single place that runs LLM-dependent org work, shared by two callers:

  • the heartbeat (a slow safety-net tick for routines, retries, missed work), and
  • immediate "kicks" — when you delegate a task, file one, hire, fire, or press
    RUN NOW, the work starts within milliseconds instead of waiting for the tick.

A module-level asyncio.Lock serialises every run so the heartbeat and an immediate
kick can never double-execute the same ticket. Kicks are coalesced: a burst of
triggers collapses into at most one extra drain, so rapid delegation can't spawn a
storm of overlapping runs. A circuit breaker pauses execution after an intelligence
core failure (bad key, logged out, network down) so we don't burn tickets on a tick
loop — a manual RUN NOW (force=True) clears it and retries on demand.
"""
import asyncio
import contextlib
import time

from . import agents, ceo, config, db, learning

_lock = asyncio.Lock()
_backoff_until: float = 0.0
_consecutive_failures = 0          # drives exponential backoff
BACKOFF_SECONDS = 30               # base; doubles per consecutive failure, capped below
BACKOFF_MAX = 300                  # never wait more than 5 min between retries

# kick coalescing state (single-threaded event loop → these checks are atomic)
_draining = False
_pending = False


def _trip_backoff(detail: str) -> None:
    """Arm the circuit breaker with exponential backoff and surface the reason."""
    global _backoff_until, _consecutive_failures
    _consecutive_failures += 1
    wait = min(BACKOFF_SECONDS * (2 ** (_consecutive_failures - 1)), BACKOFF_MAX)
    _backoff_until = time.time() + wait
    window = f"{wait} s" if wait < 60 else f"{wait // 60} min"
    db.set_setting("pipeline.last_error", detail)
    db.set_setting("pipeline.last_error_ts", str(time.time()))
    db.log_event("error", "pipeline",
                 f"Intelligence core failure — pausing execution for {window}: {detail}")


def _clear_backoff() -> None:
    global _backoff_until, _consecutive_failures
    _backoff_until = 0.0
    _consecutive_failures = 0
    db.set_setting("pipeline.last_error", "")


async def run(execute_limit: int, *, reason: str = "", force: bool = False,
              report_pending: bool = False) -> dict:
    """One full pass of the org loop. Serialised against every other run."""
    global _backoff_until
    async with _lock:
        # Assignment is cheap, non-LLM, and safe even while the core is down.
        ceo.assign_inbox_tasks()

        # Measured inside the lock so it reflects the real queue at run time.
        pending = (len(db.tasks_by_status("inbox")) + len(db.tasks_by_status("assigned"))
                   if report_pending else None)

        if force:
            _clear_backoff()
        elif time.time() < _backoff_until:
            out = {"ok": False, "skipped": "backoff", "retry_in": round(_backoff_until - time.time())}
            if report_pending:
                out["pending_before"] = pending
            return out

        # Each phase is isolated: a failure in one (e.g. the daily reflection, or a
        # single agent's provider error) must NOT abort the others. Only a genuine
        # execution failure trips the circuit breaker.
        executed = reviewed = 0
        exec_error = None

        # 1. Self-learning (observation-only) — never allowed to block real work.
        try:
            await learning.maybe_daily_reflection()
        except Exception as exc:
            db.log_event("error", "learning", f"Daily reflection failed: {str(exc)[:160]}")

        # 2. Execute assigned tickets. A provider/core failure stops THIS pass (no
        #    point hammering a dead core) and trips the breaker — but per-ticket so
        #    one bad ticket can't abort review/HR below.
        while executed < execute_limit:
            row = db.query(
                "SELECT t.* FROM tasks t JOIN agents a ON a.id = t.agent_id "
                "WHERE t.status='assigned' AND a.status='active' "
                "ORDER BY t.priority, t.created_at LIMIT 1"
            )
            if not row:
                break
            agent = db.get_agent(row[0]["agent_id"])
            try:
                await agents.execute_task(agent, row[0])
                executed += 1
            except Exception as exc:
                exec_error = str(exc)[:240]
                break

        # 3. Review — runs regardless of execution outcome so completed work always
        #    gets scored (a core-model failure here is logged, not fatal).
        try:
            for task in db.query(
                "SELECT * FROM tasks WHERE status IN ('in_progress','failed') "
                "AND result IS NOT NULL AND score IS NULL"
            ):
                await ceo.review_task(task)
                reviewed += 1
        except Exception as exc:
            # Review scores already-completed work; a transient model error here is
            # logged but must NOT trip the execution breaker (see comment above + HR pass).
            db.log_event("error", "pipeline", f"Review pass failed: {str(exc)[:160]}")

        # 4. HR — also independent.
        try:
            await ceo.hr_pass()
        except Exception as exc:
            db.log_event("error", "pipeline", f"HR pass failed: {str(exc)[:160]}")

        # 5. Project maintenance — unblock subtasks whose dependencies just finished and
        #    synthesize any project whose steps are all done. If work became ready, kick
        #    again so the next project step runs now instead of on the next heartbeat.
        try:
            from . import project
            if await project.advance():
                kick("project step ready")
        except Exception as exc:
            db.log_event("error", "project", f"Project advance failed: {str(exc)[:160]}")

        if exec_error:
            _trip_backoff(exec_error)
            out = {"ok": False, "executed": executed, "reviewed": reviewed, "error": exec_error}
        else:
            _clear_backoff()  # healthy pass
            out = {"ok": True, "executed": executed, "reviewed": reviewed}
        if report_pending:
            out["pending_before"] = pending
        return out


def kick(reason: str = "delegation") -> None:
    """Fire-and-forget: drain the queue now. Bursts coalesce into one drain."""
    global _pending
    _pending = True
    asyncio.create_task(_drain(reason))


async def _drain(reason: str) -> None:
    # Fire-and-forget target: must never let an exception escape (it would surface
    # as an unretrieved-task-exception warning). Outermost guard catches anything
    # — including a DB/log failure in the inner handler itself.
    global _draining, _pending
    if _draining:
        return  # an active drain will pick up the pending flag below
    _draining = True
    try:
        while _pending:
            _pending = False
            await run(config.MAX_DRAIN_EXECUTIONS, reason=reason)
    except Exception:
        with contextlib.suppress(Exception):
            db.log_event("error", "pipeline", "Drain failed")
    finally:
        _draining = False


async def run_now() -> dict:
    """Manual RUN NOW — force a drain, clearing any circuit-breaker backoff."""
    return await run(config.MAX_DRAIN_EXECUTIONS, reason="manual", force=True,
                     report_pending=True)


def status() -> dict:
    now = time.time()
    last_err = db.get_setting("pipeline.last_error", "")
    err_ts = db.get_setting("pipeline.last_error_ts", "")
    # treat the error as current while we're still backing off (or just after)
    fresh = bool(last_err) and (now < _backoff_until or
                                (err_ts and now - float(err_ts) < BACKOFF_MAX + 60))
    return {
        "draining": _draining,
        "backoff_active": now < _backoff_until,
        "retry_in": max(0, round(_backoff_until - now)),
        "last_error": last_err if fresh else "",
    }
