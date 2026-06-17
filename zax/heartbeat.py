"""The org heartbeat — a slow safety-net tick.

Most work now happens *immediately* when you delegate it (see pipeline.kick).
The heartbeat remains as a backstop: it fires due routines into tickets and runs
a bounded pipeline pass to catch anything missed, retry after a circuit-breaker
backoff, or pick up work created outside the chat/API (e.g. routines).
"""
import asyncio
import contextlib

from . import config, db, pipeline

_task: asyncio.Task | None = None


async def tick() -> None:
    # Routines: scheduled recurring work becomes tickets. (no LLM)
    for routine in db.due_routines():
        db.touch_routine(routine["id"])
        db.create_task(routine["name"], routine["description"], priority=2)
        db.log_event("routine", "zax", f"Routine fired: {routine['name']}")

    # Bounded pass — immediate kicks do the heavy lifting; this just backstops.
    await pipeline.run(config.MAX_EXECUTIONS_PER_TICK, reason="heartbeat")


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:
            db.log_event("error", "heartbeat", f"Heartbeat error: {exc}")
        await asyncio.sleep(config.HEARTBEAT_SECONDS)


def start() -> None:
    global _task
    _task = asyncio.get_event_loop().create_task(_loop())


async def stop() -> None:
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
