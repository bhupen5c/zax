"""Agent task execution: each hired agent runs its assigned tickets through a
tool loop (search, fetch, files, memory) and returns a deliverable for Zax to review."""
import json
import re

from . import config, db, llm, memory, tools


def _deliverable(text: str) -> str:
    """Pull the agent's answer out of a model reply, tolerating a {"final": "..."}
    wrapper even when it's truncated or has unescaped newlines (so the JSON
    scaffolding never leaks into the stored deliverable)."""
    parsed = llm.extract_json(text)
    if parsed and parsed.get("final") is not None:
        return str(parsed["final"])
    # Fallback for a {"final": "..."} that didn't fully parse (e.g. cut off by
    # the token cap): lift the value out and unescape the common sequences.
    m = re.search(r'"final"\s*:\s*"(.*?)"?\s*\}?\s*$', text, re.DOTALL)
    if m and m.group(1):
        s = m.group(1)
        for a, b in (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\")):
            s = s.replace(a, b)
        return s.strip()
    return text.strip()


async def execute_task(agent: dict, task: dict) -> None:
    db.update_task(task["id"], status="in_progress", progress=15)
    db.log_event("work", agent["name"], f"{agent['name']} started “{task['title']}”")

    # Graph-mediated memory: one block (relevant subgraph + provenance-linked facts,
    # scoped to this agent) replaces the old separate learning + graph dumps.
    mem = memory.recall_context(
        f"{task['title']} {task['description']} {agent['role']}",
        token_budget=500, agent=agent["name"],
    )
    system = (
        f"{agent['persona']}\n\n"
        f"You are {agent['name']}, {agent['title']} at {config.FOUNDER_NAME}'s organization, "
        f"reporting to Zax (the AI CEO). Complete the assigned task to a high standard — "
        f"Zax will score your work and your job depends on it."
        + (f"\n\n{mem['block']}" if mem["block"] else "")
        + f"\n\n{tools.TOOL_SPECS}"
    )
    messages = [{
        "role": "user",
        "content": f"TASK: {task['title']}\nDETAILS: {task['description'] or '(none)'}\n\n"
                   f"Produce the complete deliverable.",
    }]

    total_tokens = 0
    result = None
    try:
        for step in range(config.MAX_TOOL_STEPS):
            # progress climbs 15→80 across the tool loop; visible to the board's poll
            db.set_progress(task["id"], 15 + int((step + 0.5) / config.MAX_TOOL_STEPS * 65))
            text, tokens = await llm.chat(system, messages, model=agent["model"], max_tokens=2000)
            total_tokens += tokens
            parsed = llm.extract_json(text)
            if parsed and parsed.get("tool"):
                tool_name = str(parsed["tool"])
                args = parsed.get("args") or {}
                output = await tools.run(tool_name, args, agent_name=agent["name"])
                db.log_event("tool", agent["name"],
                             f"{agent['name']} used {tool_name}({json.dumps(args)[:120]})")
                steps_left = config.MAX_TOOL_STEPS - step - 1
                budget = (f"\n\n({steps_left} tool call{'s' if steps_left != 1 else ''} left — "
                          "gather only what you still need, then give your {\"final\": ...} answer.)"
                          if steps_left else
                          "\n\n(That was your last tool call. Give your {\"final\": ...} answer now.)")
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": f"TOOL RESULT:\n{output[:6000]}{budget}"})
                continue
            result = _deliverable(text)
            break

        # Tool budget exhausted without a final answer — force ONE no-tools
        # synthesis so the work gathered above becomes the deliverable, instead of
        # bailing with a "hit the limit" placeholder.
        if result is None:
            db.set_progress(task["id"], 85)
            messages.append({"role": "user", "content":
                "You have used your entire tool budget. Do NOT request any more tools. Using "
                "everything gathered above, write your COMPLETE final deliverable now, as "
                '{"final": "<the full answer>"}.'})
            text, tokens = await llm.chat(system, messages, model=agent["model"], max_tokens=2000)
            total_tokens += tokens
            result = _deliverable(text)

        # An empty deliverable is a failed run, not a valid result — requeue it
        # (raising routes to the except below) rather than save a blank ticket.
        if not result.strip():
            raise RuntimeError("agent produced an empty deliverable")
        # Compare-and-set: only commit if this ticket is still ours and in_progress.
        # If it was reassigned mid-run (e.g. the agent was fired), discard the
        # duplicate result rather than orphan it. progress 90 = awaiting review.
        wrote = db.finalize_task(task["id"], agent["id"],
                                 result=result.strip()[:20000], tokens_used=total_tokens, progress=90)
        if wrote:
            db.log_event("complete", agent["name"],
                         f"{agent['name']} delivered “{task['title']}” ({total_tokens} tokens)")
        else:
            db.log_event("work", agent["name"],
                         f"{agent['name']}'s ticket “{task['title']}” was reassigned mid-run "
                         f"— discarding the duplicate result")
    except Exception as exc:
        # Provider/infra failure — not the agent's fault. Requeue for retry, but
        # only if the ticket is still ours (same compare-and-set guard).
        db.finalize_task(task["id"], agent["id"], status="assigned", tokens_used=total_tokens)
        db.log_event("error", agent["name"],
                     f"{agent['name']} could not run “{task['title']}” (requeued): {str(exc)[:200]}")
        raise
    finally:
        db.add_agent_tokens(agent["id"], total_tokens)
