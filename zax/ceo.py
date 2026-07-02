"""Zax's brain: chat, task assignment, performance reviews, hiring and firing.

This is the Paperclip side (org chart, tickets, budgets, HR policy) driven by an
LLM persona, fused with the Odysseus side (chat + memory + tools) in agents.py.
"""
import asyncio
import json
import random
import re
import time
from pathlib import Path

from . import config, db, graph, learning, llm, memory, skills

PROMPTS = Path(__file__).parent / "prompts"
CODENAMES = ["Orion", "Quark", "Rune", "Helix", "Onyx", "Drift", "Ember", "Zephyr",
             "Corvus", "Lyric", "Pixel", "Cobalt", "Flux", "Mercury"]


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def hire_from_skill(skill_key: str) -> dict:
    """Hire a specialist from a skill pack (Coder, Marketer, …). Names are made
    unique if the pack's default name is already on staff."""
    pack = skills.get(skill_key)
    if not pack:
        return hire_from_template(skill_key)
    taken = {a["name"] for a in db.all_agents()}
    name = pack["name"]
    if name in taken:
        name = next((n for n in CODENAMES if n not in taken), f"Unit-{random.randint(10, 99)}")
    agent = db.create_agent(name, pack["title"], pack["role"], pack["persona"],
                            "", skill=pack["key"])
    db.log_event("hire", "zax", f"Zax hired {agent['name']} — {pack['title']} {pack['emoji']}")
    db.remember(f"Hired {agent['name']} ({pack['title']}) — {pack['category']} specialist.", "org")
    return agent


def ensure_org_seeded() -> None:
    """On first boot, Zax hires a founding team from the skill library."""
    if db.all_agents():
        return
    for key in skills.STARTER_KEYS:
        hire_from_skill(key)
    names = ", ".join(skills.get(k)["name"] for k in skills.STARTER_KEYS if skills.get(k))
    db.log_event("boot", "zax", f"Zax initialized the founding org: {names}")


# ---------------------------------------------------------------- org state

def org_state() -> dict:
    agents = db.all_agents()
    tasks = db.all_tasks(100)
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    active = [a for a in agents if a["status"] == "active"]
    return {
        "founder": config.FOUNDER_NAME,
        "provider": llm.resolve_provider(),
        "model": llm.default_model(),
        "headcount": len(active),
        "avg_performance": round(sum(a["performance"] for a in active) / len(active), 1) if active else 0,
        "tokens_spent_month": sum(a["tokens_used"] for a in agents),
        "tasks": by_status,
        "agents": [
            {
                "name": a["name"], "title": a["title"], "role": a["role"],
                "status": a["status"], "performance": round(a["performance"], 1),
                "tasks_done": a["tasks_done"], "tasks_failed": a["tasks_failed"],
                "skill": a["skill"],
                "emoji": (skills.get(a["skill"]) or {}).get("emoji", ""),
            }
            for a in agents
        ],
    }


# ---------------------------------------------------------------- chat (the Bridge)

async def chat(founder_message: str, session_id: str = "main") -> dict:
    if not db.get_session(session_id):
        session_id = "main"
    db.add_message("founder", founder_message, session_id)
    # Auto-title a fresh chat from its first message.
    sess = db.get_session(session_id)
    if sess and sess["title"] in ("New chat", "") and db.recent_messages(2, session_id):
        title = founder_message.strip().split("\n")[0][:48] or "New chat"
        db.rename_session(session_id, title)
    # Graph-mediated recall: one block routed through the knowledge graph (relevant
    # subgraph + the exact provenance-linked facts) instead of two overlapping dumps.
    # The better the graph covers the question, the less raw history we replay —
    # fewer tokens, longer effective memory.
    mem = memory.recall_context(founder_message, token_budget=600)
    memory_block = mem["block"] or "RELEVANT MEMORY: (the memory graph is still learning — keep chatting)"
    system = (
        _prompt("zax_system.txt")
        .replace("{founder}", config.FOUNDER_NAME)
        .replace("{org_state}", json.dumps(org_state(), indent=1))
        .replace("{core}", llm.core_options())
        .replace("{memory}", memory_block)
    )
    raw_turns = memory.raw_turns_for(mem["coverage"])
    history = [
        {"role": "user" if m["role"] == "founder" else "assistant", "content": m["content"]}
        for m in db.recent_messages(raw_turns, session_id)
    ]
    # Provider APIs require the first message to be from the user.
    while history and history[0]["role"] != "user":
        history.pop(0)
    try:
        # Generous cap: reasoning models (deepseek-v4-pro) spend hidden thinking tokens
        # from this same budget — 1500 can truncate the visible reply mid-action-block.
        text, tokens = await llm.chat(system, history, max_tokens=3000)
    except Exception as exc:
        text = (f"Founder, my intelligence core is offline: {str(exc)[:250]} — "
                "open Settings → Intelligence Core to fix it or switch providers.")
        tokens = 0

    reply, actions = _execute_actions(text)
    msg_id = db.add_message("zax", reply, session_id)
    saved = (f" · graph mediated {mem['n_facts']} fact(s) across {mem['n_nodes']} node(s), "
             f"replaced full history") if mem["coverage"] != "none" else ""
    db.log_event("chat", "zax", f"Zax replied to the Founder ({tokens} tokens{saved})")
    # Distil this turn into the memory graph (provenance-linked) without blocking.
    graph.schedule_exchange(founder_message, reply, str(msg_id or ""))
    # Anything Zax just set in motion (a task, a hire, a firing) executes now —
    # not on the next heartbeat. Fire-and-forget so the reply returns instantly.
    if actions:
        from . import pipeline
        pipeline.kick("chat delegation")
    return {"reply": reply, "actions": actions, "graph_context_used": mem["coverage"] != "none"}


def _execute_actions(text: str) -> tuple[str, list[dict]]:
    """Parse <action>{...}</action> blocks from Zax's reply and run them."""
    actions: list[dict] = []
    out = text
    for _ in range(3):  # at most a few actions per reply, never an unbounded loop
        start = out.find("<action>")
        if start == -1:
            break
        end = out.find("</action>", start)
        if end == -1:  # unterminated block — drop the tag and stop
            out = out[:start] + out[start + len("<action>"):]
            break
        raw = out[start + len("<action>"): end]
        try:
            act = json.loads(raw.strip())
            note = _run_action(act)
            actions.append(act)
        except Exception as exc:
            note = f"(action failed: {type(exc).__name__})"
        out = out[:start] + note + out[end + len("</action>"):]

    # Salvage a bare action JSON the model emitted WITHOUT <action> tags (models drop
    # the wrapper sometimes, and reasoning models can truncate it) — otherwise the raw
    # blob leaks into chat and the Founder's ask silently goes nowhere.
    m = _BARE_ACTION_RE.search(out)
    if m:
        end = _find_json_end(out, m.start())
        seg = out[m.start(): end if end != -1 else len(out)]
        act = llm.extract_json(seg) or {}
        if not act.get("type") and m.group(1) == "create_task":
            # Truncated mid-JSON — salvage the essentials so the work still happens.
            t = re.search(r'"title"\s*:\s*"([^"]+)"', seg)
            d = re.search(r'"description"\s*:\s*"([^"]*)', seg)
            if t:
                act = {"type": "create_task", "title": t.group(1),
                       "description": d.group(1).replace("\\n", "\n") if d else ""}
        if act.get("type"):
            try:
                note = _run_action(act)
                actions.append(act)
            except Exception as exc:
                note = f"(action failed: {type(exc).__name__})"
        else:
            note = ""  # unusable fragment — at least don't show raw JSON to the Founder
        out = out[:m.start()] + note + (out[end:] if end != -1 else "")
    return out.strip(), actions


_BARE_ACTION_RE = re.compile(r'\{\s*"type"\s*:\s*"(create_task|hire|fire)"')


def _find_json_end(s: str, start: int) -> int:
    """Index just past the brace-balanced JSON object starting at `start` (string-aware),
    or -1 if it never closes (truncated output)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _run_action(act: dict) -> str:
    kind = act.get("type")
    if kind == "create_task":
        task = db.create_task(
            act.get("title", "Untitled task"),
            act.get("description", ""),
            int(act.get("priority", 2)),
        )
        act["task_id"] = task["id"]  # so the UI can track it live in the chat
        db.log_event("task", "zax", f"Zax queued task: {task['title']}")
        return f"✓ On it now: “{task['title']}” — assigning and executing immediately, right here."
    if kind == "hire":
        # Prefer a matching specialist skill pack; fall back to a generalist.
        skill_key = act.get("skill") or (skills.match_skill(act.get("role", "")) or {}).get("key")
        agent = hire_from_skill(skill_key) if skill_key else hire_from_template(act.get("role", "generalist"))
        return f"✓ Hired {agent['name']} — {agent['title']}."
    if kind == "set_core":
        pid = str(act.get("provider") or llm.resolve_provider()).strip().lower()
        if pid not in llm.PROVIDERS:
            return f"(unknown provider: {pid})"
        spec = llm.PROVIDERS[pid]
        if not llm.is_configured(pid):
            return (f"({spec['label']} isn't configured yet — add its API key in "
                    f"Settings → Intelligence Core, then ask me again)")
        model = str(act.get("model") or "").strip()
        tier = str(act.get("reasoning") or "").strip().lower()
        if tier and not model:
            tiers = spec.get("tiers") or {}
            model = tiers.get("deep" if tier in ("deep", "high", "max", "more", "on") else "fast", "")
            if not model:
                return (f"({spec['label']} has no reasoning tiers registered — "
                        f"name a specific model instead)")
        eff = llm.set_core(pid, model)
        db.log_event("config", "zax", f"Zax switched the core to {spec['label']} · {eff}")
        return f"✓ Core switched: {spec['label']} · {eff} — effective immediately, org-wide."
    if kind == "fire":
        name = act.get("agent_name", "")
        agent = next((a for a in db.active_agents() if a["name"].lower() == name.lower()), None)
        if not agent:
            return f"(no active agent named {name})"
        reason = act.get("reason", "Founder's directive")
        db.fire_agent(agent["id"], reason)
        db.log_event("fire", "zax", f"Zax terminated {agent['name']} — {reason}")
        return f"✓ {agent['name']} has been let go. Reason on file: {reason}"
    return "(unknown action)"


# ---------------------------------------------------------------- assignment

def _match_score(task: dict, agent: dict, load: int) -> float:
    text = f"{task['title']} {task['description']}".lower()
    task_words = {w for w in "".join(c if c.isalnum() else " " for c in text).split() if len(w) > 3}
    role_words = {w for w in agent["role"].lower().replace(",", " ").split() if len(w) > 3}
    # prefix-stem match so "write" hits "writing", "research" hits "researching", etc.
    overlap = sum(
        1 for rw in role_words
        if any(tw.startswith(rw[:4]) or rw.startswith(tw[:4]) for tw in task_words)
    )
    # Strong bonus when the task hits this agent's skill-pack keywords.
    pack = skills.get(agent["skill"]) if agent.get("skill") else None
    skill_hits = sum(1 for kw in pack["keywords"] if kw in text) if pack else 0
    return overlap * 2.0 + skill_hits * 1.5 + agent["performance"] / 100.0 - load * 1.5


def assign_inbox_tasks() -> list[str]:
    notes = []
    # Rescue tickets stranded on a fired/throttled agent before assigning.
    rescued = db.rescue_orphan_tasks()
    if rescued:
        db.log_event("assign", "zax", f"Zax recovered {rescued} stranded ticket(s) for reassignment")
    agents = db.active_agents()
    if not agents:
        return notes
    loads = {
        a["id"]: len(db.query(
            "SELECT id FROM tasks WHERE agent_id=? AND status IN ('assigned','in_progress')", (a["id"],)
        ))
        for a in agents
    }
    for task in db.tasks_by_status("inbox"):
        best = max(agents, key=lambda a: _match_score(task, a, loads[a["id"]]))
        db.update_task(task["id"], status="assigned", agent_id=best["id"], progress=8)
        loads[best["id"]] += 1
        db.log_event("assign", "zax", f"Zax assigned “{task['title']}” to {best['name']}")
        notes.append(f"assigned {task['id']} -> {best['name']}")
    return notes


# ---------------------------------------------------------------- review

async def review_task(task: dict) -> None:
    agent = db.get_agent(task["agent_id"]) if task["agent_id"] else None
    system = _prompt("review.txt")
    brief = (
        f"TASK: {task['title']}\nDESCRIPTION: {task['description']}\n\n"
        f"DELIVERABLE FROM {agent['name'] if agent else 'agent'}:\n{(task['result'] or '')[:6000]}"
    )
    text, tokens = await llm.chat(system, [{"role": "user", "content": brief}], max_tokens=300)
    parsed = llm.extract_json(text) or {}
    try:
        score = max(0, min(100, int(float(parsed.get("score", 40)))))
    except (TypeError, ValueError):
        score = 40
    feedback = str(parsed.get("feedback", "No feedback."))[:500]
    failed = task["status"] == "failed" or score < 30
    db.update_task(task["id"], score=score, feedback=feedback, progress=100,
                   status="failed" if failed else "done")
    if agent:
        perf = db.update_performance(agent["id"], score, failed)
        db.add_agent_tokens(agent["id"], tokens)
        db.log_event(
            "review", "zax",
            f"Zax reviewed “{task['title']}” by {agent['name']}: {score}/100 (perf now {perf:.0f}%)",
            {"score": score, "feedback": feedback},
        )
        full = db.get_task(task["id"])
        try:  # self-learning: distill notable outcomes into skills/lessons
            await learning.learn_from_review(full, agent, score, feedback)
        except Exception as exc:
            db.log_event("error", "zax", f"Learning pass failed: {exc}")
        try:  # feed the deliverable into the knowledge graph
            await graph.ingest_task(full)
        except Exception:
            pass


# ---------------------------------------------------------------- HR: hire & fire

def hire_from_template(need: str) -> dict:
    taken = {a["name"] for a in db.all_agents()}
    name = next((n for n in CODENAMES if n not in taken), None) or f"Unit-{random.randint(10, 99)}"
    agent = db.create_agent(
        name, f"{need.title()} Specialist"[:40], need,
        f"You are {name}, a specialist in {need}. You deliver exactly what's asked, directly and "
        f"concisely — the answer first, no preamble or filler, respecting any explicit format or "
        f"length constraint.",
        "",
    )
    db.log_event("hire", "zax", f"Zax hired {agent['name']} — {agent['title']}")
    db.remember(f"Hired {agent['name']} ({agent['title']}) for: {need}", "org")
    return agent


async def hire_with_llm(need: str) -> dict:
    staff = ", ".join(f"{a['name']} ({a['role']})" for a in db.active_agents()) or "nobody"
    system = _prompt("hire.txt").replace("{need}", need).replace("{staff}", staff)
    try:
        text, _ = await llm.chat(system, [{"role": "user", "content": f"Hiring brief for: {need}"}], max_tokens=400)
        spec = llm.extract_json(text) or {}
        taken = {a["name"] for a in db.all_agents()}
        name = str(spec.get("name", "")).strip().title() or "Nova"
        if name in taken:
            name = next((n for n in CODENAMES if n not in taken), f"Unit-{random.randint(10, 99)}")
        agent = db.create_agent(
            name,
            str(spec.get("title", f"{need.title()} Specialist"))[:60],
            str(spec.get("role", need))[:200],
            str(spec.get("persona", f"You are {name}, a specialist in {need}."))[:1000],
            "",
        )
        db.log_event("hire", "zax", f"Zax hired {agent['name']} — {agent['title']}")
        db.remember(f"Hired {agent['name']} ({agent['title']}) for: {need}", "org")
        return agent
    except Exception:
        return hire_from_template(need)


async def hr_pass() -> None:
    """Zax's personnel review: fire underperformers, hire when the backlog demands it."""
    # Un-throttle agents whose monthly budget has rolled over to a new month.
    month = time.strftime("%Y-%m")
    for a in db.query("SELECT * FROM agents WHERE status='throttled'"):
        if a["budget_month"] != month:
            db.execute("UPDATE agents SET status='active', tokens_used=0, budget_month=? WHERE id=?",
                       (month, a["id"]))
            db.log_event("hire", "zax", f"Zax reinstated {a['name']} — budget reset for {month}")

    agents = db.active_agents()
    for a in agents:
        total = a["tasks_done"] + a["tasks_failed"]
        if total >= config.MIN_TASKS_BEFORE_FIRE and a["performance"] < config.FIRE_THRESHOLD:
            reason = (f"Performance {a['performance']:.0f}% after {total} tasks — "
                      f"below the {config.FIRE_THRESHOLD:.0f}% bar.")
            db.fire_agent(a["id"], reason)
            db.log_event("fire", "zax", f"Zax terminated {a['name']} — {reason}")
            db.remember(f"Fired {a['name']}: {reason}", "org")
        elif a["tokens_used"] > a["token_budget"] and a["status"] == "active":
            db.execute("UPDATE agents SET status='throttled' WHERE id=?", (a["id"],))
            # Hand the throttled agent's un-started tickets back so work continues.
            db.requeue_assigned(a["id"])
            db.log_event("throttle", "zax",
                         f"Zax throttled {a['name']} — monthly token budget exhausted "
                         f"({a['tokens_used']:,}/{a['token_budget']:,})")

    agents = db.active_agents()
    inbox = db.tasks_by_status("inbox")
    backlog = len(inbox) + len(db.tasks_by_status("assigned"))
    if agents and len(agents) < config.MAX_HEADCOUNT:
        if backlog > len(agents) * config.HIRE_BACKLOG_PER_AGENT:
            # Hire the specialist the backlog actually needs, if a pack fits and
            # isn't already on staff; otherwise an LLM-drafted generalist.
            have = {a["skill"] for a in agents}
            needed = None
            for t in inbox:
                pack = skills.match_skill(f"{t['title']} {t['description']}")
                if pack and pack["key"] not in have:
                    needed = pack["key"]
                    break
            if needed:
                hire_from_skill(needed)
            else:
                await hire_with_llm("general execution — clearing a growing task backlog")
    elif not agents:
        hire_from_skill("researcher")


# ---------------------------------------------------------------- greeting

GREETINGS = [
    "Systems online. Welcome back, Founder.",
    "Zax online. The org has been holding the line in your absence, Founder.",
    "Good {tod}, Founder. Your organization stands ready.",
]


async def greeting() -> str:
    hour = time.localtime().tm_hour
    tod = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    state = org_state()
    fallback = (
        f"Good {tod}, {config.FOUNDER_NAME}. Zax online. "
        f"{state['headcount']} agents on staff, org performance {state['avg_performance']}%, "
        f"{state['tasks'].get('inbox', 0) + state['tasks'].get('assigned', 0)} tasks in the pipeline. "
        "What are we building today?"
    )
    if llm.resolve_provider() == "mock":
        return fallback
    try:
        system = (
            "You are ZAX, a deep-voiced AI CEO greeting your Founder as the app boots. "
            "One or two sentences. Confident, cinematic, warm but precise. Address them as Founder. "
            f"It is {tod}. Org state: {json.dumps(state)}"
        )
        text, _ = await asyncio.wait_for(
            llm.chat(system, [{"role": "user", "content": "I just opened the app. Greet me."}], max_tokens=120),
            timeout=30,
        )
        return text.strip() or fallback
    except Exception:
        return fallback
