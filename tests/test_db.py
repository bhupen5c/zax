"""Unit tests for the persistence layer (zax/db.py)."""
import time

from zax import db


# ---------------------------------------------------------------- agents

def test_create_and_get_agent():
    a = db.create_agent("Atlas", "Researcher", "research", "persona", "", skill="researcher")
    assert a["name"] == "Atlas" and a["skill"] == "researcher"
    assert db.get_agent(a["id"])["title"] == "Researcher"
    assert a["status"] == "active" and a["performance"] == 75.0


def test_active_vs_all_agents():
    a = db.create_agent("A", "t", "r", "p", "")
    db.create_agent("B", "t", "r", "p", "")
    db.fire_agent(a["id"], "test")
    assert len(db.active_agents()) == 1
    assert len(db.all_agents()) == 2


def test_update_performance_ema():
    a = db.create_agent("A", "t", "r", "p", "")
    perf = db.update_performance(a["id"], 100, failed=False)
    assert 75 < perf < 100  # EMA between old (75) and new (100)
    assert db.get_agent(a["id"])["tasks_done"] == 1


def test_token_budget_resets_on_new_month():
    a = db.create_agent("A", "t", "r", "p", "")
    db.execute("UPDATE agents SET tokens_used=500, budget_month='2000-01' WHERE id=?", (a["id"],))
    db.add_agent_tokens(a["id"], 10)  # new month -> reset then add
    assert db.get_agent(a["id"])["tokens_used"] == 10


# ---------------------------------------------------------------- tasks

def test_task_lifecycle():
    t = db.create_task("Do X", "details", priority=1)
    assert t["status"] == "inbox" and t["progress"] == 0
    db.update_task(t["id"], status="done", score=90, progress=100)
    assert db.get_task(t["id"])["status"] == "done"


def test_finalize_task_cas_blocks_when_reassigned():
    """finalize_task must NOT write if the ticket was yanked mid-run."""
    a = db.create_agent("A", "t", "r", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], status="in_progress", agent_id=a["id"])
    # success path writes
    assert db.finalize_task(t["id"], a["id"], result="done", progress=90) is True
    # now simulate reassignment (status changed) -> CAS refuses
    db.update_task(t["id"], status="inbox", agent_id=None)
    assert db.finalize_task(t["id"], a["id"], result="stale") is False
    assert db.get_task(t["id"])["result"] == "done"  # stale not written


def test_fire_requeues_only_assigned_not_in_progress():
    a = db.create_agent("A", "t", "r", "p", "")
    assigned = db.create_task("assigned")
    inflight = db.create_task("inflight")
    db.update_task(assigned["id"], status="assigned", agent_id=a["id"])
    db.update_task(inflight["id"], status="in_progress", agent_id=a["id"])
    db.fire_agent(a["id"], "test")
    assert db.get_task(assigned["id"])["status"] == "inbox"        # requeued
    assert db.get_task(inflight["id"])["status"] == "in_progress"  # left to finish


def test_rescue_orphan_tasks():
    a = db.create_agent("A", "t", "r", "p", "")
    t = db.create_task("orphan")
    db.update_task(t["id"], status="assigned", agent_id=a["id"])
    db.fire_agent(a["id"], "gone")  # agent inactive, task already requeued by fire
    # force an orphan: assigned to an inactive agent
    db.update_task(t["id"], status="assigned", agent_id=a["id"])
    moved = db.rescue_orphan_tasks()
    assert moved >= 1 and db.get_task(t["id"])["status"] == "inbox"


def test_set_progress_is_monotonic():
    t = db.create_task("X")
    db.set_progress(t["id"], 50)
    db.set_progress(t["id"], 20)  # lower -> ignored
    assert db.get_task(t["id"])["progress"] == 50
    db.set_progress(t["id"], 80)
    assert db.get_task(t["id"])["progress"] == 80


# ---------------------------------------------------------------- chat sessions

def test_sessions_crud_and_main_protected():
    main = db.get_session("main")
    assert main is not None
    s = db.create_session("Test")
    db.add_message("founder", "hi", s["id"])
    db.add_message("zax", "hello", s["id"])
    msgs = db.recent_messages(10, s["id"])
    assert len(msgs) == 2 and msgs[0]["role"] == "founder"
    # other session isolated
    assert len(db.recent_messages(10, "main")) == 0
    db.rename_session(s["id"], "Renamed")
    assert db.get_session(s["id"])["title"] == "Renamed"
    db.delete_session(s["id"])
    assert db.get_session(s["id"]) is None


# ---------------------------------------------------------------- memory (FTS)

def test_memory_recall_and_reinforce():
    db.remember("Founder prefers concise answers", kind="note")
    db.remember("The org ships fast", kind="org")
    hits = db.recall("concise answers")
    assert any("concise" in h["content"] for h in hits)
    # reinforcing the same fact bumps importance, not a duplicate
    before = db.list_memories()
    db.remember("The org ships fast", kind="org")
    after = db.list_memories()
    assert len(after) == len(before)


def test_recall_handles_punctuation_only_query():
    assert db.recall("!!! ??? ...") == []


# ---------------------------------------------------------------- graph

def test_graph_nodes_and_edges():
    db.upsert_node("n1", "Payments", "concept", "billing", "chat")
    db.upsert_node("n2", "Stripe", "tool", "processor", "chat")
    db.upsert_edge("n1", "n2", "uses", "EXTRACTED", "")
    assert db.graph_node_count() == 2
    assert db.graph_edge_count() == 1
    db.delete_node("n1")
    assert db.graph_node_count() == 1
    assert db.graph_edge_count() == 0  # edge removed with node


# ---------------------------------------------------------------- events

def test_events_since_snapshot_is_newest_first():
    for i in range(10):
        db.log_event("complete", "AgentX", f"task {i}")
    snap = db.events_since(0, limit=5)
    assert len(snap) == 5
    # newest first, and only the most recent 5
    assert snap[0]["message"] == "task 9"
    assert snap[-1]["message"] == "task 5"


def test_events_since_incremental_never_drops_oldest_unseen():
    # A burst larger than the poll limit must not skip the events just above after_id.
    for i in range(100):
        db.log_event("complete", "AgentX", f"task {i}")
    first_id = db.events_since(0, limit=100)[-1]["id"]  # id of "task 0"
    batch = db.events_since(first_id, limit=40)
    ids = [e["id"] for e in batch]
    assert len(ids) == 40
    # oldest unseen (task 1, id == first_id + 1) must be present, not discarded
    assert min(ids) == first_id + 1
    # caller advances by max id; remaining events are picked up next poll (no gap)
    nxt = db.events_since(max(ids), limit=40)
    assert min(e["id"] for e in nxt) == max(ids) + 1


# ---------------------------------------------------------------- settings

def test_settings_upsert():
    db.set_setting("k", "v1")
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"
    assert db.get_setting("missing", "default") == "default"
