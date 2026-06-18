"""The Memory Mediator — the graph as a single router for all long-term context.

Before this module, every prompt carried TWO independent memory dumps:
  • a keyword FTS recall over db.memories  → the `{memory}` block, and
  • a graphify subgraph over the knowledge graph → the `{graph}` block.
They didn't know about each other, so prompts paid for redundant, overlapping
context, and the graph could never pull the *exact* fact behind a relevant node.

`recall_context()` makes the graph the mediator. One retrieval:
  1. Seed + expand the relevant subgraph (graphify) → the relationship layer.
  2. Follow each subgraph node's PROVENANCE links back to the exact memories it
     was distilled from → the fact layer (precise, not re-derived).
  3. Backfill with an FTS recall whose query is *expanded by the subgraph's own
     labels* — graph-mediated recall, not a blind parallel dump.
  4. Dedupe (a fact already visible in the relationship layer isn't repeated) and
     render ONE block inside a single token budget.

The result replaces both old blocks, so the same (or better) context costs far
fewer tokens — and its `coverage` lets callers trim raw history to match.
"""
from . import db, graph

# Memory kinds worth surfacing into a working prompt.
_FACT_KINDS = ["skill", "lesson", "note", "org", "report"]

_HEADER = (
    "RELEVANT MEMORY (retrieved via the knowledge graph — treat as known; prior "
    "conversations and full history need not be replayed):"
)


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())[:48]


def recall_context(question: str, *, token_budget: int = 600, agent: str = "") -> dict:
    """Single graph-mediated context block for prompt injection.

    Returns {block, coverage, n_facts, n_nodes}. `coverage` is one of
    'strong' | 'partial' | 'none' and is meant to drive how much raw history the
    caller still needs to send (strong coverage → fewer raw turns)."""
    empty = {"block": "", "coverage": "none", "n_facts": 0, "n_nodes": 0}
    if not (question or "").strip():
        return empty

    char_budget = max(300, token_budget * 3)  # graphify's ~3 chars/token convention

    # 1+2. Graph subgraph (relationships) + its node ids for provenance routing.
    g = graph.retrieve(question, token_budget=int(token_budget * 0.6))
    graph_text = g["text"]
    node_ids, labels = g["node_ids"], g["labels"]

    # The relationship layer already spends part of the budget; facts get the rest.
    fact_char_budget = max(180, char_budget - len(graph_text))

    # 3. Facts: provenance-linked memories first (exact), then graph-expanded recall.
    prov = db.memories_by_ids(db.memory_ids_for_nodes(node_ids, limit=12))
    expansion = (question + " " + " ".join(labels[:8])).strip()
    backfill = db.recall(expansion, limit=6, kinds=_FACT_KINDS, agent=agent or "")

    seen_ids: set[int] = set()
    seen_keys: set[str] = set()
    # Don't repeat a fact whose text is already visible in the relationship layer.
    graph_norm = _norm(graph_text)
    facts: list[str] = []
    used = 0
    for m in prov + backfill:
        if m["id"] in seen_ids:
            continue
        seen_ids.add(m["id"])
        # Agent-scoping: an agent sees only company-wide + its own memories.
        if agent and m["agent"] and m["agent"] != agent:
            continue
        key = _norm(m["content"])
        if not key or key in seen_keys:
            continue
        if key[:24] and key[:24] in graph_norm:
            continue  # already implied by the graph layer
        seen_keys.add(key)
        line = f"- [{m['kind']}] {m['content'].strip()}"
        if used + len(line) > fact_char_budget and facts:
            break
        facts.append(line)
        used += len(line)
        if len(facts) >= 6:
            break

    if not graph_text and not facts:
        return empty

    # 4. Render one block.
    parts = [_HEADER]
    if graph_text:
        parts.append("• How things connect:\n" + graph_text)
    if facts:
        parts.append("• Facts on record:\n" + "\n".join(facts))
    block = "\n\n".join(parts)

    if graph_text and (facts or len(labels) >= 4):
        coverage = "strong"
    else:
        coverage = "partial"
    return {"block": block, "coverage": coverage, "n_facts": len(facts),
            "n_nodes": len(node_ids)}


# Balanced raw-history trim: the better the graph covers the question, the fewer
# verbatim turns we still need to replay. (chat default ceiling is 16.)
_RAW_TURNS = {"strong": 4, "partial": 6, "none": 16}


def raw_turns_for(coverage: str) -> int:
    return _RAW_TURNS.get(coverage, 16)
