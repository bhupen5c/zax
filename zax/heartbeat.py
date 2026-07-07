"""Routine scheduler — the ONLY time-based tick.

Zax is otherwise fully event-driven: work runs the instant it is delegated
(pipeline.kick), and each task runs straight through to completion — there is no
task 'heartbeat' polling loop. This loop exists solely to (a) turn due recurring
routines into tasks and (b) recover work stranded by a crash/restart. Both simply
kick the pipeline, which then executes immediately.
"""
import asyncio
import contextlib

from . import config, db, pipeline

_task: asyncio.Task | None = None


async def tick() -> None:
    work = False
    # Routines: scheduled recurring work becomes tasks (no LLM here).
    for routine in db.due_routines():
        db.touch_routine(routine["id"])
        db.create_task(routine["name"], routine["description"], priority=2)
        db.log_event("routine", "zax", f"Routine fired: {routine['name']}")
        work = True
    # Recover anything left ready/assigned by a restart so it isn't stranded.
    if db.tasks_by_status("inbox") or db.tasks_by_status("assigned"):
        work = True
    # Event-driven: we don't run a pipeline pass here — we just kick, and the kick
    # drains the queue to completion right away.
    if work:
        pipeline.kick("scheduler")


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:
            db.log_event("error", "scheduler", f"Scheduler error: {exc}")
        await asyncio.sleep(config.SCHEDULER_SECONDS)


def start() -> None:
    global _task
    _task = asyncio.get_event_loop().create_task(_loop())


async def stop() -> None:
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
