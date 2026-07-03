"""Zax Memory Graph — embeds the graphify knowledge-graph engine
(https://github.com/safishamsi/graphify, vendored under vendor/graphify).

Why this exists: instead of replaying the whole chat history to the model on every
turn (expensive, and it overflows the context window), Zax distills conversations,
tasks, and memories into a persistent knowledge graph of entities + relationships.
At query time graphify's own retrieval pipeline (TF-IDF node scoring → seed pick →
BFS subgraph → token-budgeted render) pulls back only the *relevant* subgraph as a
compact text block, which is injected into the prompt. The model reads facts from
the graph rather than from raw transcript — far fewer tokens, longer effective memory.

The graph is the source of truth in SQLite (graph_nodes / graph_edges); we
materialize a NetworkX graph on demand and reuse graphify's serve.py functions
verbatim for retrieval, and export graphify-compatible graph.json for the UI and
for `python -m graphify.serve data/graph.json`.
"""
import asyncio
import json
import re
import sys

from . import config, db, llm

# Make the vendored graphify package importable (it only needs networkx at import).
_VENDOR = str(config.ROOT / "vendor" / "graphify")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

try:
    import networkx as nx
    from networkx.readwrite import json_graph
    from graphify import serve as gserve

    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # networkx missing, or vendor dir absent
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

KINDS = ("person", "project", "preference", "fact", "tool", "concept", "agent", "task")
STOP = set("""the a an and or but if then this that these those is are was were be been being
to of in on at for with from by as it its into about over under your you i we our us me my
he she they them his her their will would can could should have has had do does did not no
yes get got make made just like want need please thanks hello hi okay ok zax founder""".split())


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s[:60] or "node"


# ---------------------------------------------------------------- materialize

def materialize() -> "nx.Graph":
    """Build a NetworkX graph (graphify's node schema) from the SQLite store."""
    G = nx.Graph()
    for n in db.graph_nodes():
        G.add_node(
            n["id"],
            label=n["label"],
            kind=n["kind"],
            summary=n["summary"],
            source_file=n["source"],
            source_location="",
            community=n["kind"],
            weight=n["weight"],
        )
    for e in db.graph_edges():
        if G.has_node(e["src"]) and G.has_node(e["tgt"]):
            G.add_edge(e["src"], e["tgt"], relation=e["relation"],
                       confidence=e["confidence"], context=e["context"], weight=e["weight"])
    return G


def graph_json() -> dict:
    """graphify-compatible node_link JSON, also written to data/graph.json."""
    if not AVAILABLE:
        return {"nodes": [], "links": [], "directed": False, "multigraph": False, "graph": {}}
    G = materialize()
    data = json_graph.node_link_data(G, edges="links")
    try:
        (config.DATA_DIR / "graph.json").write_text(json.dumps(data))
    except Exception:
        pass
    return data


def stats() -> dict:
    nodes = db.graph_nodes()
    G = materialize() if AVAILABLE and nodes else None
    god = []
    if G is not None and G.number_of_nodes():
        god = sorted(G.degree(), key=lambda d: -d[1])[:6]
        god = [{"id": nid, "label": G.nodes[nid].get("label", nid), "degree": deg}
               for nid, deg in god if deg > 0]
    by_kind: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    return {
        "available": AVAILABLE,
        "import_error": IMPORT_ERROR,
        "nodes": db.graph_node_count(),
        "edges": db.graph_edge_count(),
        "by_kind": by_kind,
        "god_nodes": god,
        "engine": "graphify (vendored)",
    }


# ---------------------------------------------------------------- retrieval

def _augment_seeds(G, terms: list[str]) -> list[str]:
    """Graphify scores node *labels* only — great for entity-name queries, but a
    question phrased by attribute ('which database?', 'payment provider?') names no
    entity. Recover recall by also matching query terms against node summaries,
    kinds, and the relation text on incident edges."""
    termset = {t for t in terms if t not in STOP and len(t) > 2}
    if not termset:
        return []
    hits: dict[str, float] = {}
    for nid, d in G.nodes(data=True):
        hay = f"{d.get('summary', '')} {d.get('kind', '')}".lower()
        if any(t in hay for t in termset):
            hits[nid] = max(hits.get(nid, 0), G.degree(nid) + 1)
    for u, v, d in G.edges(data=True):
        rel = str(d.get("relation", "")).lower()
        if any(t in rel for t in termset):
            hits[u] = max(hits.get(u, 0), G.degree(u))
            hits[v] = max(hits.get(v, 0), G.degree(v))
    return [nid for nid, _ in sorted(hits.items(), key=lambda x: -x[1])[:4]]


def _seed_subgraph(G, question: str, depth: int = 2):
    terms = gserve._query_terms(question)
    if not terms:
        return None, set(), []
    scored = gserve._score_nodes(G, terms)
    seeds = gserve._pick_seeds(scored)
    if len(seeds) < 2:  # weak label match — fold in summary/relation matches
        seeds = list(dict.fromkeys(seeds + _augment_seeds(G, terms)))
    if not seeds:
        return None, set(), []
    nodes, edges = gserve._bfs(G, seeds, depth)
    return seeds, nodes, edges


def context_block(question: str, token_budget: int = 600) -> str:
    """Compact, relevant subgraph rendered for prompt injection. '' if nothing matches.

    This is the token-saver: it replaces raw chat history with just the facts the
    model needs, capped at token_budget using graphify's renderer."""
    if not AVAILABLE or db.graph_node_count() == 0:
        return ""
    G = materialize()
    seeds, nodes, edges = _seed_subgraph(G, question)
    if not nodes:
        return ""
    return gserve._subgraph_to_text(G, nodes, edges, token_budget=token_budget, seeds=seeds)


def retrieve(question: str, token_budget: int = 500, depth: int = 2) -> dict:
    """Mediator primitive: the relevant subgraph as both rendered text AND its node
    ids/labels, so the caller can route from those nodes to their source memories.

    Returns {text, node_ids, labels, seeds} — empty lists/'' when nothing matches."""
    empty = {"text": "", "node_ids": [], "labels": [], "seeds": []}
    if not AVAILABLE or db.graph_node_count() == 0:
        return empty
    G = materialize()
    seeds, nodes, edges = _seed_subgraph(G, question, depth=depth)
    if not nodes:
        return empty
    text = gserve._subgraph_to_text(G, nodes, edges, token_budget=token_budget, seeds=seeds)
    labels = [G.nodes[n].get("label", n) for n in nodes if G.has_node(n)]
    return {"text": text, "node_ids": list(nodes), "labels": labels, "seeds": list(seeds)}


def query(question: str, token_budget: int = 1200) -> dict:
    """Answer 'what connects X to Y?' style questions from the graph (Graph tab)."""
    if not AVAILABLE:
        return {"ok": False, "error": f"graph engine unavailable: {IMPORT_ERROR}"}
    if db.graph_node_count() == 0:
        return {"ok": True, "text": "The memory graph is empty — chat with Zax to populate it.",
                "seeds": [], "node_count": 0}
    G = materialize()
    seeds, nodes, edges = _seed_subgraph(G, question, depth=3)
    if not nodes:
        return {"ok": True, "text": "No matching nodes found.", "seeds": [], "node_count": 0}
    text = gserve._subgraph_to_text(G, nodes, edges, token_budget=token_budget, seeds=seeds)
    return {"ok": True, "text": text, "node_count": len(nodes),
            "seeds": [G.nodes[s].get("label", s) for s in seeds]}


def path(a: str, b: str) -> dict:
    """graphify `path` — shortest chain of relationships between two entities."""
    if not AVAILABLE or db.graph_node_count() == 0:
        return {"ok": True, "text": "The memory graph is empty.", "hops": 0}
    G = materialize()
    starts = gserve._find_node(G, a)
    ends = gserve._find_node(G, b)
    if not starts or not ends:
        missing = a if not starts else b
        return {"ok": True, "text": f"Couldn't find “{missing}” in the graph.", "hops": 0}
    try:
        nodes = nx.shortest_path(G, starts[0], ends[0])
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"ok": True, "text": f"No path connects “{a}” and “{b}”.", "hops": 0}
    steps = []
    for u, v in zip(nodes, nodes[1:]):
        rel = (G.get_edge_data(u, v) or {}).get("relation", "→")
        steps.append(f"{G.nodes[u].get('label', u)} —{rel}→ {G.nodes[v].get('label', v)}")
    return {"ok": True, "text": "\n".join(steps), "hops": len(nodes) - 1}


def explain(node: str) -> dict:
    """graphify `explain` — a node and its immediate relationships."""
    if not AVAILABLE or db.graph_node_count() == 0:
        return {"ok": True, "text": "The memory graph is empty."}
    G = materialize()
    found = gserve._find_node(G, node)
    if not found:
        return {"ok": True, "text": f"Couldn't find “{node}” in the graph."}
    nid = found[0]
    d = G.nodes[nid]
    lines = [f"{d.get('label', nid)} [{d.get('kind', 'concept')}]"]
    if d.get("summary"):
        lines.append(d["summary"])
    for nbr in G.neighbors(nid):
        rel = (G.get_edge_data(nid, nbr) or {}).get("relation", "related to")
        lines.append(f"  —{rel}→ {G.nodes[nbr].get('label', nbr)}")
    return {"ok": True, "text": "\n".join(lines), "degree": G.degree(nid)}


# ---------------------------------------------------------------- extraction

def _persist(nodes: list[dict], edges: list[dict], source: str) -> list[str]:
    """Write extracted entities/relationships into the graph store.

    Returns the list of node ids touched (so the caller can link provenance)."""
    label_to_id: dict[str, str] = {}
    touched: list[str] = []
    for n in nodes:
        label = str(n.get("name") or n.get("label") or "").strip()[:80]
        if not label or label.lower() in STOP:
            continue
        kind = str(n.get("kind") or "concept").lower()
        kind = kind if kind in KINDS else "concept"
        nid = _slug(label)
        label_to_id[label.lower()] = nid
        db.upsert_node(nid, label, kind, str(n.get("summary") or "")[:240], source)
        touched.append(nid)
    for e in edges:
        s = str(e.get("source") or "").strip().lower()
        t = str(e.get("target") or "").strip().lower()
        if not s or not t or s == t:
            continue
        sid = label_to_id.get(s) or _slug(s)
        tid = label_to_id.get(t) or _slug(t)
        # ensure endpoints exist even if only named inside a relationship
        if s not in label_to_id and s not in STOP:
            db.upsert_node(sid, str(e.get("source")).strip()[:80], "concept", "", source)
            touched.append(sid)
        if t not in label_to_id and t not in STOP:
            db.upsert_node(tid, str(e.get("target")).strip()[:80], "concept", "", source)
            touched.append(tid)
        db.upsert_edge(sid, tid, str(e.get("relation") or "related to")[:60],
                       str(e.get("confidence") or "EXTRACTED").upper(), source)
    return list(dict.fromkeys(touched))


def _fallback_extract(text: str) -> tuple[list[dict], list[dict]]:
    """Zero-token deterministic extraction: salient terms + co-occurrence edges.

    Captures multi-word Capitalized phrases (proper nouns / project names) and
    distinctive lowercase keywords, links co-occurring terms as INFERRED edges."""
    phrases = re.findall(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3})\b", text)
    keywords = [w for w in re.findall(r"\b[a-z][a-z0-9]{4,}\b", text.lower()) if w not in STOP]
    seen, terms = set(), []
    for term in [p.strip() for p in phrases] + keywords:
        key = term.lower()
        if key in STOP or key in seen or len(term) < 3:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 6:
            break
    nodes = [{"name": t, "kind": "concept"} for t in terms]
    edges = [{"source": terms[0], "target": t, "relation": "related to", "confidence": "INFERRED"}
             for t in terms[1:]] if len(terms) > 1 else []
    return nodes, edges


async def _llm_extract(text: str) -> tuple[list[dict], list[dict]]:
    system = (PROMPTS_OK and _EXTRACT_PROMPT) or ""
    if not system:
        return _fallback_extract(text)
    try:
        out, _ = await llm.chat(system, [{"role": "user", "content": text[:4000]}], max_tokens=1500)
        parsed = llm.extract_json(out) or {}
        nodes = parsed.get("entities") or []
        edges = parsed.get("relationships") or []
        if not nodes:
            return _fallback_extract(text)
        for e in edges:
            e.setdefault("confidence", "EXTRACTED")
        return nodes, edges
    except Exception:
        return _fallback_extract(text)


from pathlib import Path  # noqa: E402

_PROMPT_PATH = Path(__file__).parent / "prompts" / "graph_extract.txt"
try:
    _EXTRACT_PROMPT = _PROMPT_PATH.read_text()
    PROMPTS_OK = True
except Exception:
    _EXTRACT_PROMPT, PROMPTS_OK = "", False


async def ingest_text(text: str, source: str, ref: tuple[str, str] | None = None) -> int:
    """Distil `text` into the graph. When `ref=(ref_kind, ref_id)` is given, every
    node touched is provenance-linked to that source row, so the graph can later
    route retrieval straight back to the exact memory/task. Returns node count."""
    if not AVAILABLE or not text or not text.strip():
        return 0
    use_llm = llm.resolve_provider() != "mock"
    nodes, edges = await _llm_extract(text) if use_llm else _fallback_extract(text)
    touched = _persist(nodes, edges, source)
    if ref and touched:
        ref_kind, ref_id = ref
        for nid in touched:
            db.link_provenance(nid, ref_kind, str(ref_id))
    return len(touched)


async def ingest_exchange(founder_msg: str, zax_reply: str, message_id: str = "") -> None:
    """Fire-and-forget: distil one chat turn into the graph (best effort)."""
    try:
        ref = ("message", message_id) if message_id else None
        await ingest_text(f"Founder said: {founder_msg}\nZax replied: {zax_reply}", "chat", ref)
    except Exception as exc:
        db.log_event("error", "graph", f"Graph ingest failed: {str(exc)[:160]}")


# Keep a reference to pending background tasks so they don't get garbage-collected
# mid-flight, which causes "Task was destroyed but it is pending" crashes.
_pending_graph_tasks: set[asyncio.Task] = set()


def schedule_exchange(founder_msg: str, zax_reply: str, message_id: str = "") -> None:
    """Schedule ingestion without blocking the chat response."""
    if not AVAILABLE:
        return
    try:
        task = asyncio.get_event_loop().create_task(
            ingest_exchange(founder_msg, zax_reply, message_id))
        _pending_graph_tasks.add(task)
        task.add_done_callback(_pending_graph_tasks.discard)
    except RuntimeError:
        pass


def schedule_memory(mem_id: int, content: str, kind: str) -> None:
    """Fire-and-forget: index a freshly-stored memory into the graph (provenance-linked)
    so the mediator can route to it immediately, without blocking the writer."""
    if not AVAILABLE or not mem_id:
        return
    try:
        task = asyncio.get_event_loop().create_task(ingest_memory(mem_id, content, kind))
        _pending_graph_tasks.add(task)
        task.add_done_callback(_pending_graph_tasks.discard)
    except RuntimeError:
        pass


async def ingest_task(task: dict) -> None:
    if not AVAILABLE:
        return
    body = f"Task: {task['title']}. {task.get('description', '')}. Outcome: {(task.get('result') or '')[:600]}"
    ref = ("task", task["id"]) if task.get("id") else None
    try:
        await ingest_text(body, f"task:{task['title'][:40]}", ref)
    except Exception:
        pass


async def ingest_memory(mem_id: int, content: str, kind: str) -> None:
    """Index a single institutional memory (skill/lesson/report/coaching) into the
    graph the moment it's stored, provenance-linked so the mediator can route to it."""
    if not AVAILABLE or not mem_id:
        return
    try:
        await ingest_text(content, f"memory:{kind}", ("memory", str(mem_id)))
    except Exception:
        pass


async def rebuild() -> dict:
    """Wipe and rebuild the whole graph from chat history, tasks, and memories."""
    if not AVAILABLE:
        return {"ok": False, "error": f"graph engine unavailable: {IMPORT_ERROR}"}
    db.clear_graph()
    msgs = db.recent_messages(200)
    for i in range(0, len(msgs) - 1):
        if msgs[i]["role"] == "founder" and msgs[i + 1]["role"] == "zax":
            await ingest_text(f"Founder said: {msgs[i]['content']}\nZax replied: {msgs[i + 1]['content']}", "chat")
    for t in db.all_tasks(100):
        if t.get("result"):
            await ingest_task(t)
    for m in db.list_memories(limit=100):
        await ingest_text(m["content"], f"memory:{m['kind']}", ("memory", str(m["id"])))
    graph_json()
    return {"ok": True, **stats()}
