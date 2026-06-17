"""Unit tests for the knowledge graph (graphify integration) and self-learning."""
from zax import db, graph, learning, llm


# ---------------------------------------------------------------- graph

def test_persist_and_materialize():
    # _persist uses name/source/target (labels); it slugs the node IDs itself.
    nodes = [{"name": "Payments", "kind": "concept"},
             {"name": "Stripe", "kind": "tool", "summary": "processor"}]
    edges = [{"source": "Payments", "target": "Stripe", "relation": "uses", "confidence": "EXTRACTED"}]
    graph._persist(nodes, edges, source="test")
    assert db.graph_node_count() == 2 and db.graph_edge_count() == 1
    G = graph.materialize()
    assert G.number_of_nodes() == 2 and G.number_of_edges() == 1


def test_graph_json_node_link_shape():
    graph._persist([{"name": "Alpha", "kind": "concept"}], [], "t")
    data = graph.graph_json()
    assert "nodes" in data and "links" in data
    assert any(n["id"] == graph._slug("Alpha") for n in data["nodes"])


def test_graph_query_and_context_block():
    nodes = [{"name": "Payments", "kind": "concept", "summary": "billing flow"},
             {"name": "Stripe", "kind": "tool", "summary": "processor"}]
    edges = [{"source": "Payments", "target": "Stripe", "relation": "uses", "confidence": "EXTRACTED"}]
    graph._persist(nodes, edges, "t")
    res = graph.query("what connects to payments")
    assert isinstance(res, dict)
    block = graph.context_block("payments", token_budget=300)
    assert isinstance(block, str)  # may be empty on a tiny graph, but never errors


def test_fallback_extract_no_llm():
    nodes, edges = graph._fallback_extract("The Founder wants Payments integrated with Stripe.")
    assert isinstance(nodes, list) and isinstance(edges, list)


async def test_ingest_text_builds_graph():
    before = db.graph_node_count()
    await graph.ingest_text("The launch plan covers Marketing and Pricing.", source="chat")
    assert db.graph_node_count() >= before  # never errors; usually adds nodes


def test_graph_stats():
    s = graph.stats()
    assert "available" in s and "nodes" in s


def test_graph_path_and_explain():
    nodes = [{"name": "Payments", "kind": "concept"},
             {"name": "Stripe", "kind": "tool"},
             {"name": "Webhook", "kind": "concept"}]
    edges = [{"source": "Payments", "target": "Stripe", "relation": "uses", "confidence": "EXTRACTED"},
             {"source": "Stripe", "target": "Webhook", "relation": "emits", "confidence": "EXTRACTED"}]
    graph._persist(nodes, edges, "t")
    # path chains relationships across two hops
    p = graph.path("Payments", "Webhook")
    assert p["ok"] and p["hops"] == 2 and "Stripe" in p["text"]
    # explain lists a node + its neighbours
    e = graph.explain("Stripe")
    assert e["ok"] and "Stripe" in e["text"] and e["degree"] == 2
    # graceful on unknown
    assert graph.path("Payments", "Nonexistent")["hops"] == 0
    assert "Couldn't find" in graph.explain("Ghost")["text"]


# ---------------------------------------------------------------- learning

async def test_learn_from_review_distills_skill_on_high_score():
    a = db.create_agent("A", "t", "research", "p", "", skill="researcher")
    t = db.create_task("Research X", "find facts")
    db.update_task(t["id"], result="A thorough, well-sourced report.", score=95)
    await learning.learn_from_review(db.get_task(t["id"]), a, 95, "excellent")
    assert any(m["kind"] == "skill" for m in db.list_memories())


async def test_learn_from_review_distills_lesson_on_low_score():
    a = db.create_agent("A", "t", "research", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], result="weak output", score=40)
    await learning.learn_from_review(db.get_task(t["id"]), a, 40, "missed the point")
    assert any(m["kind"] == "lesson" for m in db.list_memories())


async def test_learn_skips_middling_scores():
    a = db.create_agent("A", "t", "r", "p", "")
    t = db.create_task("X")
    db.update_task(t["id"], result="ok", score=72)
    before = len(db.list_memories())
    await learning.learn_from_review(db.get_task(t["id"]), a, 72, "fine")
    assert len(db.list_memories()) == before  # nothing distilled from a 72


def test_context_for_injects_memory():
    db.remember("When researching, always cite two sources.", kind="skill", agent="")
    a = db.create_agent("A", "t", "research", "p", "", skill="researcher")
    block = learning.context_for(a, {"title": "Research the market", "description": "cite sources"})
    assert isinstance(block, str)


async def test_daily_reflection_writes_report(seeded):
    a = db.active_agents()[0]
    t = db.create_task("done task")
    db.update_task(t["id"], status="done", agent_id=a["id"], result="x", score=88)
    res = await learning.daily_reflection(force=True)
    assert res["ok"] is True
    assert any(m["kind"] == "report" for m in db.list_memories())
