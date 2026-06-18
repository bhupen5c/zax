"""Tests for the graph-mediated memory system (zax/memory.py + provenance)."""
from zax import db, graph, memory


# ---------------------------------------------------------------- provenance (db layer)

def test_remember_returns_id_and_reinforces():
    mid = db.remember("Stripe is our payment provider.", kind="org")
    assert isinstance(mid, int) and mid > 0
    # storing the same content reinforces the existing row, returning the same id
    assert db.remember("Stripe is our payment provider.", kind="org") == mid


def test_provenance_links_node_to_memory():
    mid = db.remember("We bill monthly via Stripe.", kind="org")
    db.link_provenance("stripe", "memory", str(mid))
    db.link_provenance("stripe", "memory", str(mid))  # idempotent (bumps weight)
    assert db.memory_ids_for_nodes(["stripe"]) == [mid]
    assert db.memories_by_ids([mid])[0]["content"] == "We bill monthly via Stripe."
    assert db.memory_ids_for_nodes([]) == []
    assert db.memories_by_ids([]) == []


def test_clear_and_delete_node_drop_provenance():
    mid = db.remember("X uses Y.", kind="org")
    db.link_provenance("xnode", "memory", str(mid))
    db.delete_node("xnode")
    assert db.memory_ids_for_nodes(["xnode"]) == []


# ---------------------------------------------------------------- mediator

def _seed_payment_graph():
    mid = db.remember("Stripe is our chosen payment provider; webhooks confirm charges.",
                      kind="org", importance=2.0)
    graph._persist(
        [{"name": "Payments", "kind": "concept"},
         {"name": "Stripe", "kind": "tool", "summary": "processor"},
         {"name": "Webhook", "kind": "concept"}],
        [{"source": "Payments", "target": "Stripe", "relation": "uses", "confidence": "EXTRACTED"},
         {"source": "Stripe", "target": "Webhook", "relation": "emits", "confidence": "EXTRACTED"}],
        "t")
    db.link_provenance(graph._slug("Stripe"), "memory", str(mid))
    return mid


def test_recall_context_routes_through_graph_to_provenance_fact():
    _seed_payment_graph()
    out = memory.recall_context("Stripe payments webhook", token_budget=400)
    assert out["block"]                         # produced a block
    assert out["n_nodes"] > 0                   # graph mediated retrieval
    assert "payment provider" in out["block"]   # exact provenance fact was pulled in
    assert out["coverage"] in ("strong", "partial")


def test_recall_context_respects_token_budget():
    _seed_payment_graph()
    out = memory.recall_context("Stripe payments webhook", token_budget=120)
    # ~3 chars/token; allow headroom for the header but it must stay bounded.
    assert len(out["block"]) <= 120 * 3 + 200


def test_recall_context_empty_is_safe():
    out = memory.recall_context("nothing has been stored yet")
    assert out == {"block": "", "coverage": "none", "n_facts": 0, "n_nodes": 0}
    assert memory.recall_context("")["block"] == ""


def test_recall_context_agent_scoping():
    # An agent-private memory must not leak to other agents via the mediator.
    mid = db.remember("Atlas-only: always cite two sources.", kind="lesson", agent="Atlas")
    db.link_provenance("sources", "memory", str(mid))
    graph._persist([{"name": "Sources", "kind": "concept"}], [], "t")
    other = memory.recall_context("sources", token_budget=300, agent="Lyra")
    assert "Atlas-only" not in other["block"]
    mine = memory.recall_context("sources", token_budget=300, agent="Atlas")
    assert "Atlas-only" in mine["block"]


def test_raw_turns_decrease_with_coverage():
    # Balanced trim: better graph coverage → fewer raw turns replayed.
    assert memory.raw_turns_for("strong") == 4
    assert memory.raw_turns_for("partial") == 6
    assert memory.raw_turns_for("none") == 16
