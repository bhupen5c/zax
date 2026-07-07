"""Multi-step projects: decompose a goal into a dependency graph of subtasks, run
them in order across the org, then synthesize the final deliverable.

This is the Paperclip 'company runs a project' layer on top of single-task agents:
plan_and_start() breaks a goal into blocked/ready subtasks; advance() (called each
pipeline pass) unblocks subtasks whose dependencies are done and synthesizes a
project once every subtask is finished.
"""
from pathlib import Path

from . import db, llm, skills

PROMPTS = Path(__file__).parent / "prompts"


def _skill_menu() -> str:
    return "\n".join(f"  {s['key']}: {s['role']}" for s in skills.SKILLS)


async def plan_and_start(goal: str) -> dict | None:
    """Decompose a goal into subtasks (with dependencies) and launch the project.
    Returns the project row, or None if the goal didn't decompose (caller can fall
    back to a single task)."""
    system = (PROMPTS / "plan_project.txt").read_text().replace("{skills}", _skill_menu())
    text, _ = await llm.chat(system, [{"role": "user", "content": f"GOAL: {goal}"}], max_tokens=2000)
    plan = llm.extract_json(text) or {}
    subs = plan.get("subtasks") or []
    # A single-step "project" is just a task — let the normal path handle it.
    if len(subs) < 2:
        return None

    project = db.create_project(goal)
    step_to_id: dict[int, str] = {}
    created: list[tuple[dict, dict]] = []
    for i, s in enumerate(subs, 1):
        step = int(s.get("step", i))
        # A skill hint in the description keeps the skill-aware assigner on target.
        skill = str(s.get("skill", "")).strip()
        desc = str(s.get("description", ""))[:4000]
        if skill and skills.get(skill):
            desc = f"[{skills.get(skill)['role']}] {desc}"
        has_deps = bool(s.get("deps"))
        t = db.create_task(str(s.get("title", "Subtask"))[:300], desc, priority=1,
                           project_id=project["id"], status="blocked" if has_deps else "inbox")
        step_to_id[step] = t["id"]
        created.append((t, s))

    # Second pass: now that every step has an id, resolve deps (step numbers -> task ids).
    for t, s in created:
        deps = [step_to_id[int(d)] for d in (s.get("deps") or []) if int(d) in step_to_id]
        if deps:
            db.execute("UPDATE tasks SET deps=? WHERE id=?", (",".join(deps), t["id"]))

    db.log_event("project", "zax",
                 f"Zax launched project “{goal[:60]}” — {len(subs)} steps across the org")
    return project


async def advance() -> bool:
    """Unblock ready subtasks and synthesize finished projects. Returns True if
    anything changed (so the pipeline can kick the newly-ready work immediately)."""
    changed = False
    for p in db.active_projects():
        tasks = db.project_tasks(p["id"])
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            if t["status"] != "blocked":
                continue
            deps = [d for d in (t["deps"] or "").split(",") if d]
            statuses = [by_id.get(d, {}).get("status") for d in deps]
            if all(s == "done" for s in statuses):
                db.set_task_status(t["id"], "inbox")
                db.log_event("project", "zax", f"Unblocked “{t['title'][:50]}” — dependencies met")
                changed = True
            elif any(s == "failed" for s in statuses):
                db.set_task_status(t["id"], "failed")  # a dead dependency kills the branch
                changed = True

        tasks = db.project_tasks(p["id"])  # refresh after unblocking
        if tasks and all(t["status"] in ("done", "failed") for t in tasks):
            await _synthesize(p, tasks)
            changed = True
    return changed


async def _synthesize(project: dict, tasks: list[dict]) -> None:
    parts = "\n\n".join(
        f"### {t['title']}\n{(t['result'] or '(no result)')[:2000]}" for t in tasks)
    system = (
        "You are Zax, the AI CEO, assembling your team's completed subtask deliverables into ONE "
        "cohesive final deliverable for the Founder's goal. Integrate the pieces — resolve overlaps, "
        "order it logically, and present a finished product the Founder can use directly. Open with a "
        "1-2 sentence executive summary, then the integrated deliverable. No filler.")
    user = f"GOAL: {project['goal']}\n\nCOMPLETED SUBTASKS:\n{parts}"
    try:
        text, _ = await llm.chat(system, [{"role": "user", "content": user}], max_tokens=4000)
        result = text.strip()[:20000]
    except Exception as exc:
        result = f"(synthesis unavailable: {str(exc)[:120]})\n\n" + parts[:15000]
    done = sum(1 for t in tasks if t["status"] == "done")
    db.update_project(project["id"], status="done", result=result)
    db.log_event("project", "zax",
                 f"Project “{project['goal'][:60]}” complete — {done}/{len(tasks)} steps, final deliverable ready")


def overview() -> list[dict]:
    """Projects with their subtask progress, for the UI."""
    out = []
    for p in db.all_projects():
        tasks = db.project_tasks(p["id"])
        out.append({
            **p,
            "steps": [{"id": t["id"], "title": t["title"], "status": t["status"],
                       "score": t["score"]} for t in tasks],
            "done": sum(1 for t in tasks if t["status"] == "done"),
            "total": len(tasks),
        })
    return out
