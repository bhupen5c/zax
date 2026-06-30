"""Agent task execution: each hired agent runs its assigned tickets through a
tool loop (search, fetch, files, memory) and returns a deliverable for Zax to review."""
import json
import re

from . import config, db, llm, memory, tools


# Shared output discipline injected into EVERY agent (specialist or generic). This is
# what keeps deliverables tight and send-ready: blind-judge benchmarking showed Zax's
# work was correct but lost on padding (memo letterheads, restated prompts, gratuitous
# disclaimers, multiple variants), ignored constraints (e.g. "3 lines"), and weak/invented
# citations. Each rule below maps to one of those failure modes.
OUTPUT_RULES = (
    "OUTPUT DISCIPLINE — you are graded on this, and sloppiness here costs you the score:\n"
    "1. Deliver ONLY what was asked, directly. No letterhead, no \"Prepared by / To / From / "
    "Date / Subject:\" headers, no cover note, no preamble, no restating the task, no sign-off. "
    "Your first line is already the answer.\n"
    "2. Obey every explicit constraint EXACTLY: line counts (\"3 lines\"), item counts (\"max 8 "
    "items\"), word/character limits, and \"one X\". A 3-line email is exactly one email of three "
    "lines — not a paragraph plus three alternates.\n"
    "3. Lead with the answer or recommendation, then the substance that backs it. Cut filler and "
    "hedging — but NEVER cut substance. Be SPECIFIC and CONCRETE: name real tools, products, "
    "examples and (sourced) numbers instead of generic statements, and include every part the task "
    "explicitly asks for (e.g. a clearly labelled one-line takeaway, each requested section). "
    "\"Concise\" means no padding — NOT thin or generic.\n"
    "4. Do NOT add disclaimers, caveats, or \"this is not professional advice\" notes unless the "
    "task explicitly asks for them.\n"
    "5. Facts and figures: cite authoritative PRIMARY sources (official docs, standards, reputable "
    "benchmarks/indices like DB-Engines) by real URL — never invent a URL or source. Every URL you "
    "cite MUST be copied verbatim from a web_search/fetch_url result in THIS run; never type or "
    "recall a URL from memory (that is how fabricated/misspelled domains slip in). State a precise "
    "figure (latency, %, $, throughput, count) ONLY if it came from a source you actually retrieved "
    "this run; otherwise describe it qualitatively or label it \"vendor-claimed / unverified\". "
    "Inventing precise-looking numbers is a correctness failure.\n"
    "6. Use a compact Markdown table when comparing 3+ options or showing period-over-period "
    "numbers — it is scanned far faster than prose.\n"
    "7. Produce ONE version of the requested artifact. Offer alternatives only if the task asks for options."
)


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


def _is_leaked_tool_call(s: str) -> bool:
    """True if a 'deliverable' is really an unparsed tool-call JSON leaking through —
    e.g. a write_file whose large code payload (raw newlines/quotes) broke JSON parsing,
    so extract_json couldn't see the "tool" key and the raw object fell through. We must
    never store that as the answer; force a clean no-tools synthesis instead."""
    return bool(re.match(r'\s*\{\s*"tool"\s*:', s))


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
        f"Zax will score your work and your job depends on it.\n\n"
        f"{OUTPUT_RULES}"
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
            candidate = _deliverable(text)
            if _is_leaked_tool_call(candidate):
                break  # leave result=None so the forced no-tools synthesis runs below
            result = candidate
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
