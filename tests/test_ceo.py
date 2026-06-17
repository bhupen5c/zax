"""Unit tests for Zax's brain (zax/ceo.py)."""
from zax import ceo, db, skills


# ---------------------------------------------------------------- seeding & hiring

def test_org_seeds_founding_team():
    ceo.ensure_org_seeded()
    names = {a["name"] for a in db.active_agents()}
    # starter packs: coder, marketer, researcher, ops
    assert len(db.active_agents()) == len(skills.STARTER_KEYS)
    assert any(db.get_agent(a["id"])["skill"] == "coder" for a in db.active_agents())


def test_hire_from_skill():
    a = ceo.hire_from_skill("designer")
    assert a["skill"] == "designer" and "Designer" in a["title"]


def test_hire_from_skill_unique_names():
    a1 = ceo.hire_from_skill("coder")
    a2 = ceo.hire_from_skill("coder")  # same pack again
    assert a1["name"] != a2["name"]  # second gets a codename


# ---------------------------------------------------------------- action parsing

def test_execute_actions_create_task():
    reply, actions = ceo._execute_actions(
        'On it. <action>{"type":"create_task","title":"Research X","priority":1}</action>')
    assert len(actions) == 1 and actions[0]["type"] == "create_task"
    assert "On it" in reply and "<action>" not in reply
    assert db.tasks_by_status("inbox")[0]["title"] == "Research X"


def test_execute_actions_malformed_is_safe():
    reply, actions = ceo._execute_actions('<action>{bad json</action> text')
    assert actions == [] and "action failed" in reply


def test_execute_actions_unterminated_is_safe():
    reply, actions = ceo._execute_actions('<action>{"never":"closed"')
    assert isinstance(reply, str) and actions == []


def test_execute_actions_fire_unknown_agent():
    reply, actions = ceo._execute_actions(
        '<action>{"type":"fire","agent_name":"Ghost","reason":"x"}</action>')
    assert "no active agent" in reply.lower()


# ---------------------------------------------------------------- assignment

def test_assignment_is_skill_aware():
    ceo.ensure_org_seeded()  # includes coder (Caspian) + marketer (Lumen)
    t = db.create_task("Write python code to parse a CSV file", "implement a parser")
    ceo.assign_inbox_tasks()
    assigned = db.get_task(t["id"])
    assert assigned["status"] == "assigned"
    agent = db.get_agent(assigned["agent_id"])
    assert agent["skill"] == "coder"  # routed to the engineer, not a random agent


def test_assignment_rescues_orphans():
    a = db.create_agent("A", "t", "general", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], status="assigned", agent_id=a["id"])
    db.fire_agent(a["id"], "gone")
    db.update_task(t["id"], status="assigned", agent_id=a["id"])  # re-orphan
    db.create_agent("B", "t", "general", "p", "")  # an active agent to take it
    ceo.assign_inbox_tasks()
    assert db.get_task(t["id"])["status"] == "assigned"
    assert db.get_agent(db.get_task(t["id"])["agent_id"])["status"] == "active"


# ---------------------------------------------------------------- review

async def test_review_sets_score_status_progress():
    a = db.create_agent("A", "t", "general", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"], result="a deliverable")
    await ceo.review_task(db.get_task(t["id"]))
    cur = db.get_task(t["id"])
    assert cur["score"] is not None
    assert cur["status"] in ("done", "failed") and cur["progress"] == 100


# ---------------------------------------------------------------- chat

async def test_chat_returns_reply_and_logs():
    res = await ceo.chat("Give me a status report", session_id="main")
    assert res["reply"] and isinstance(res["actions"], list)
    assert len(db.recent_messages(10, "main")) == 2  # founder + zax


async def test_chat_auto_titles_session():
    s = db.create_session()
    await ceo.chat("Plan our product launch strategy", session_id=s["id"])
    assert db.get_session(s["id"])["title"].startswith("Plan our product")


# ---------------------------------------------------------------- HR

async def test_hr_fires_underperformer():
    a = db.create_agent("Dud", "t", "general", "p", "")
    # 3 tasks, terrible performance
    db.execute("UPDATE agents SET performance=20, tasks_done=3 WHERE id=?", (a["id"],))
    await ceo.hr_pass()
    assert db.get_agent(a["id"])["status"] == "fired"


async def test_hr_throttles_over_budget_and_requeues():
    a = db.create_agent("Spender", "t", "general", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], status="assigned", agent_id=a["id"])
    db.execute("UPDATE agents SET tokens_used=999999, token_budget=1 WHERE id=?", (a["id"],))
    await ceo.hr_pass()
    assert db.get_agent(a["id"])["status"] == "throttled"
    assert db.get_task(t["id"])["status"] == "inbox"  # work handed back
