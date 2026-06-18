"""Self-learning: Zax turns the org's own work into institutional memory.

Two loops, both powered by the active AI model:
1. Per-review distillation — every scored task with a clearly good (>=85) or
   clearly bad (<60) outcome is distilled into a reusable `skill` or `lesson`.
2. Daily reflection — once a day Zax reviews the last 24h of org activity,
   writes an executive report, extracts lessons, and leaves coaching notes for
   individual agents.

Everything lands in the persistent memory bank (db.memories) and is injected
back into agent prompts by context_for(), closing the loop.
"""
import time
from pathlib import Path

from . import db, graph, llm, memory

PROMPTS = Path(__file__).parent / "prompts"

SKILL_BAR = 85
LESSON_BAR = 60


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


# ---------------------------------------------------------------- injection

def context_for(agent: dict, task: dict) -> str:
    """Memory block injected into an agent's system prompt before it works.

    Thin wrapper over the graph mediator (scoped to this agent), kept for callers
    that only need the rendered block."""
    probe = f"{task['title']} {task['description']} {agent['role']}"
    return memory.recall_context(probe, token_budget=500, agent=agent["name"])["block"]


def _remember(content: str, *, kind: str, agent: str = "", importance: float = 1.0) -> None:
    """Store a memory AND index it into the graph (provenance-linked) so the mediator
    can route to it immediately — institutional knowledge enters the graph live."""
    mem_id = db.remember(content, kind=kind, agent=agent, importance=importance)
    graph.schedule_memory(mem_id, content, kind)


# ---------------------------------------------------------------- per-review

async def learn_from_review(task: dict, agent: dict, score: int, feedback: str) -> None:
    """Distill a skill (great work) or lesson (failure) from a reviewed task."""
    if LESSON_BAR <= score < SKILL_BAR:
        return  # unremarkable outcomes teach nothing worth storing
    kind = "skill" if score >= SKILL_BAR else "lesson"
    system = _prompt("learn_review.txt").replace("{kind}", kind)
    brief = (
        f"TASK: {task['title']}\nDETAILS: {task['description'][:500]}\n"
        f"AGENT: {agent['name']} ({agent['role']})\nSCORE: {score}/100\n"
        f"CEO FEEDBACK: {feedback}\nDELIVERABLE EXCERPT:\n{(task['result'] or '')[:1500]}"
    )
    text, _ = await llm.chat(system, [{"role": "user", "content": brief}], max_tokens=200)
    parsed = llm.extract_json(text) or {}
    content = str(parsed.get("text", "")).strip()
    if not content or len(content) < 15:
        return
    _remember(content, kind=kind, agent=agent["name"],
              importance=2.0 if kind == "lesson" else 1.5)
    db.log_event("learn", "zax", f"Zax recorded a {kind} from “{task['title']}”: {content[:90]}")


# ---------------------------------------------------------------- daily reflection

def _activity_since(cutoff: float) -> tuple[list[dict], list[dict]]:
    tasks = db.query(
        "SELECT * FROM tasks WHERE updated_at > ? AND score IS NOT NULL ORDER BY updated_at",
        (cutoff,),
    )
    events = db.query(
        "SELECT * FROM events WHERE ts > ? AND kind IN ('hire','fire','throttle') ORDER BY ts",
        (cutoff,),
    )
    return tasks, events


async def maybe_daily_reflection() -> None:
    today = time.strftime("%Y-%m-%d")
    if db.get_setting("learning.last_reflection_day") == today:
        return
    last_attempt = float(db.get_setting("learning.last_reflection_attempt") or 0)
    if time.time() - last_attempt < 3600:
        return  # a recent attempt failed; retry at most hourly
    tasks, _ = _activity_since(time.time() - 86400)
    if not tasks:
        return  # nothing to learn from yet; try again next tick
    db.set_setting("learning.last_reflection_attempt", str(time.time()))
    await daily_reflection()


async def daily_reflection(force: bool = False) -> dict:
    cutoff = time.time() - 86400
    tasks, events = _activity_since(cutoff)
    if not tasks and not force:
        return {"ok": False, "error": "no reviewed tasks in the last 24h"}

    agents = {a["id"]: a for a in db.all_agents()}
    lines = []
    for t in tasks:
        who = agents.get(t["agent_id"], {}).get("name", "?")
        lines.append(f"- “{t['title']}” by {who}: {t['score']}/100 ({t['status']}) — {t['feedback'] or ''}")
    for e in events:
        lines.append(f"- ORG EVENT: {e['message']}")
    staff = ", ".join(f"{a['name']} (perf {a['performance']:.0f}%)"
                      for a in db.active_agents()) or "none"
    brief = (
        "ACTIVITY (last 24h):\n" + ("\n".join(lines) or "- nothing") +
        f"\n\nCURRENT STAFF: {staff}"
    )

    text, _ = await llm.chat(_prompt("reflect.txt"),
                             [{"role": "user", "content": brief}], max_tokens=700)
    parsed = llm.extract_json(text) or {}
    report = str(parsed.get("report", "")).strip()
    lessons = [str(x).strip() for x in (parsed.get("lessons") or []) if str(x).strip()]
    coaching = parsed.get("coaching") or {}

    if report:
        _remember(f"[{time.strftime('%Y-%m-%d')}] {report}", kind="report", importance=1.2)
    for lesson in lessons[:3]:
        _remember(lesson, kind="lesson", importance=2.0)
    valid_names = {a["name"] for a in db.active_agents()}
    for name, note in list(coaching.items())[:5]:
        if str(name) in valid_names and str(note).strip():
            _remember(f"Coaching for {name}: {str(note).strip()}", kind="lesson",
                      agent=str(name), importance=2.0)

    db.set_setting("learning.last_reflection_day", time.strftime("%Y-%m-%d"))
    db.set_setting("learning.last_reflection_ts", str(time.time()))
    db.log_event(
        "learn", "zax",
        f"Zax completed his daily reflection — {len(lessons)} lesson(s), "
        f"{len(coaching)} coaching note(s). {report[:120]}",
        {"report": report, "lessons": lessons, "coaching": coaching},
    )
    return {"ok": True, "report": report, "lessons": lessons, "coaching": coaching,
            "tasks_reviewed": len(tasks)}


def status() -> dict:
    ts = db.get_setting("learning.last_reflection_ts")
    return {
        "last_reflection": float(ts) if ts else None,
        "last_reflection_day": db.get_setting("learning.last_reflection_day") or None,
        "counts": db.memory_counts(),
    }
