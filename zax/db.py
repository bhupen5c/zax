"""Persistence for Zax: org, tasks, events, chat, memory, graph, routines.

DUAL BACKEND:
  • SQLite (default) — a single local file, guarded by a lock. Local + tests.
  • Postgres — when config.DATABASE_URL is set (e.g. Supabase). Same Python API;
    queries are placeholder-translated (?→%s) and a few backend-specific spots
    (auto-increment via RETURNING, full-text recall via tsvector) branch on PG.
"""
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from . import config

PG = bool(config.DATABASE_URL)  # Postgres mode when a connection string is set

_lock = threading.Lock()
_conn: Any = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    persona TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | throttled | fired
    performance REAL NOT NULL DEFAULT 75.0,
    tasks_done INTEGER NOT NULL DEFAULT 0,
    tasks_failed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    token_budget INTEGER NOT NULL,
    budget_month TEXT NOT NULL DEFAULT '',
    skill TEXT NOT NULL DEFAULT '',             -- skill-pack key this agent was hired from
    hired_at REAL NOT NULL,
    fired_at REAL,
    fired_reason TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'inbox',       -- inbox | assigned | in_progress | done | failed | blocked
    priority INTEGER NOT NULL DEFAULT 2,        -- 1 high, 2 normal, 3 low
    agent_id TEXT,
    result TEXT,
    score INTEGER,
    feedback TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0,        -- 0-100, live during execution
    project_id TEXT NOT NULL DEFAULT '',        -- '' = standalone; else a multi-step project
    deps TEXT NOT NULL DEFAULT '',              -- comma-sep task ids that must finish first
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | done | failed
    result TEXT NOT NULL DEFAULT '',            -- synthesized final deliverable
    created REAL NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    created REAL NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,                          -- founder | zax
    content TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'main'
);

CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    last_run REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'concept',   -- person | project | preference | fact | tool | concept | agent | task
    summary TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0,
    created REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
    src TEXT NOT NULL,
    tgt TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related to',
    confidence TEXT NOT NULL DEFAULT 'INFERRED',   -- EXTRACTED | INFERRED | AMBIGUOUS (graphify tags)
    context TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0,
    created REAL NOT NULL,
    PRIMARY KEY (src, tgt, relation)
);

-- Provenance: links a graph node back to the exact source rows it was distilled
-- from. This is what lets the graph act as a MEDIATOR — a node surfaced by
-- retrieval can fetch the precise underlying fact (a memory/task) instead of the
-- model re-deriving it from replayed history.
CREATE TABLE IF NOT EXISTS graph_provenance (
    node_id  TEXT NOT NULL,
    ref_kind TEXT NOT NULL,   -- 'memory' | 'task' | 'message'
    ref_id   TEXT NOT NULL,
    weight   REAL NOT NULL DEFAULT 1.0,
    created  REAL NOT NULL,
    PRIMARY KEY (node_id, ref_kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_provenance_node ON graph_provenance(node_id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',   -- note | org | lesson | skill | report
    agent TEXT NOT NULL DEFAULT '',      -- '' = company-wide, else an agent's name
    importance REAL NOT NULL DEFAULT 1.0,
    uses INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last_used REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, content='memories', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    task_title TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    command TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    output TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '',       -- JSON sidecar (e.g. {"branch": "self-update/..."})
    created REAL NOT NULL,
    resolved REAL NOT NULL DEFAULT 0
);
"""

# Postgres-native schema (Supabase). Identity columns replace AUTOINCREMENT; a GIN
# tsvector index replaces FTS5; DOUBLE PRECISION replaces REAL.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, role TEXT NOT NULL,
    persona TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    performance DOUBLE PRECISION NOT NULL DEFAULT 75.0, tasks_done INTEGER NOT NULL DEFAULT 0,
    tasks_failed INTEGER NOT NULL DEFAULT 0, tokens_used BIGINT NOT NULL DEFAULT 0,
    token_budget BIGINT NOT NULL, budget_month TEXT NOT NULL DEFAULT '',
    skill TEXT NOT NULL DEFAULT '', hired_at DOUBLE PRECISION NOT NULL,
    fired_at DOUBLE PRECISION, fired_reason TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'inbox', priority INTEGER NOT NULL DEFAULT 2, agent_id TEXT,
    result TEXT, score INTEGER, feedback TEXT, tokens_used BIGINT NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0, project_id TEXT NOT NULL DEFAULT '',
    deps TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    result TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts DOUBLE PRECISION NOT NULL,
    kind TEXT NOT NULL, actor TEXT NOT NULL, message TEXT NOT NULL, data TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New chat',
    created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts DOUBLE PRECISION NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL, last_run DOUBLE PRECISION NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'concept',
    summary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0, created DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_edges (
    src TEXT NOT NULL, tgt TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'related to',
    confidence TEXT NOT NULL DEFAULT 'INFERRED', context TEXT NOT NULL DEFAULT '',
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0, created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (src, tgt, relation)
);
CREATE TABLE IF NOT EXISTS graph_provenance (
    node_id TEXT NOT NULL, ref_kind TEXT NOT NULL, ref_id TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0, created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (node_id, ref_kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_provenance_node ON graph_provenance(node_id);
CREATE TABLE IF NOT EXISTS memories (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note', agent TEXT NOT NULL DEFAULT '',
    importance DOUBLE PRECISION NOT NULL DEFAULT 1.0, uses INTEGER NOT NULL DEFAULT 0,
    created DOUBLE PRECISION NOT NULL, last_used DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING GIN (to_tsvector('english', content));
CREATE TABLE IF NOT EXISTS approvals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, agent TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '', task_title TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL, command TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', output TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '',
    created DOUBLE PRECISION NOT NULL, resolved DOUBLE PRECISION NOT NULL DEFAULT 0
);
"""


def _connect_pg():
    """Open (or reopen) the Postgres connection, ensure schema + the 'main' session."""
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in PG_SCHEMA.split(";") if s.strip()]:
            cur.execute(stmt)
        cur.execute("SELECT 1 FROM chat_sessions WHERE id='main'")
        if not cur.fetchone():
            now = time.time()
            cur.execute("INSERT INTO chat_sessions (id, title, created, updated) "
                        "VALUES ('main','Main',%s,%s)", (now, now))
    return conn


def connect():
    global _conn
    if PG:
        if _conn is None or getattr(_conn, "closed", False):
            _conn = _connect_pg()
        return _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        # migrate the original flat FTS memory table into the structured store
        old = _conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
        ).fetchone()
        if old:
            _conn.execute(
                "INSERT INTO memories (content, kind, agent, importance, created, last_used)"
                " SELECT content, kind, '', 1.0, CAST(ts AS REAL), CAST(ts AS REAL) FROM memory"
            )
            _conn.execute("DROP TABLE memory")
        # add the progress column to pre-existing task tables
        tcols = {r["name"] for r in _conn.execute("PRAGMA table_info(tasks)")}
        if "progress" not in tcols:
            _conn.execute("ALTER TABLE tasks ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            _conn.execute("UPDATE tasks SET progress=100 WHERE status IN ('done','failed')")
        # add the skill column to pre-existing agent tables
        acols = {r["name"] for r in _conn.execute("PRAGMA table_info(agents)")}
        if "skill" not in acols:
            _conn.execute("ALTER TABLE agents ADD COLUMN skill TEXT NOT NULL DEFAULT ''")
        # add multi-step project columns to pre-existing task tables
        if "project_id" not in tcols:
            _conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            _conn.execute("ALTER TABLE tasks ADD COLUMN deps TEXT NOT NULL DEFAULT ''")
        # add the meta sidecar to pre-existing approvals tables (self-update branch name)
        apcols = {r["name"] for r in _conn.execute("PRAGMA table_info(approvals)")}
        if apcols and "meta" not in apcols:
            _conn.execute("ALTER TABLE approvals ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
        # add per-session chat: session_id on messages + a default 'main' session
        mcols = {r["name"] for r in _conn.execute("PRAGMA table_info(messages)")}
        if "session_id" not in mcols:
            _conn.execute("ALTER TABLE messages ADD COLUMN session_id TEXT NOT NULL DEFAULT 'main'")
        now = time.time()
        if not _conn.execute("SELECT 1 FROM chat_sessions WHERE id='main'").fetchone():
            _conn.execute("INSERT INTO chat_sessions (id, title, created, updated) VALUES ('main','Main',?,?)",
                          (now, now))
        _conn.commit()
    return _conn


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _pgsql(sql: str) -> str:
    # Our queries use ? placeholders and never a literal '?', so a plain swap to
    # psycopg's %s is safe. (No literal '%' appears in any query either.)
    return sql.replace("?", "%s")


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        conn = connect()
        if PG:
            with conn.cursor() as cur:
                cur.execute(_pgsql(sql), params)
                return cur.fetchall()  # dict_row → list[dict]
        return _rows(conn.execute(sql, params))


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        conn = connect()
        if PG:
            with conn.cursor() as cur:
                cur.execute(_pgsql(sql), params)
            return
        conn.execute(sql, params)
        conn.commit()


def _insert_returning_id(sql: str, params: tuple) -> int:
    """INSERT and return the new auto-increment id (RETURNING on PG, lastrowid on SQLite)."""
    with _lock:
        conn = connect()
        if PG:
            with conn.cursor() as cur:
                cur.execute(_pgsql(sql) + " RETURNING id", params)
                return int(cur.fetchone()["id"])
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid)


def _execute_rowcount(sql: str, params: tuple) -> int:
    """Run a write and return the number of affected rows (for compare-and-set)."""
    with _lock:
        conn = connect()
        if PG:
            with conn.cursor() as cur:
                cur.execute(_pgsql(sql), params)
                return cur.rowcount
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- events

def log_event(kind: str, actor: str, message: str, data: Any = None) -> None:
    execute(
        "INSERT INTO events (ts, kind, actor, message, data) VALUES (?,?,?,?,?)",
        (time.time(), kind, actor, message, json.dumps(data) if data is not None else None),
    )


def events_since(after_id: int = 0, limit: int = 100) -> list[dict]:
    # after_id == 0 is an initial snapshot: the most-recent `limit` events, newest first.
    if after_id <= 0:
        return query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    # Incremental poll: return the OLDEST unseen events first (capped), so a burst
    # larger than `limit` never silently drops the events just above after_id — the
    # caller advances past this window and picks up the rest next poll. Displayed
    # newest-first to match feed/ticker consumers.
    return query(
        "SELECT * FROM (SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?) AS e "
        "ORDER BY id DESC",  # subquery alias 'e' required by Postgres (harmless in SQLite)
        (after_id, limit),
    )


# ---------------------------------------------------------------- agents

def create_agent(name: str, title: str, role: str, persona: str, model: str,
                 skill: str = "") -> dict:
    aid = new_id()
    execute(
        "INSERT INTO agents (id, name, title, role, persona, model, token_budget, budget_month, skill, hired_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, name, title, role, persona, model, config.DEFAULT_TOKEN_BUDGET,
         time.strftime("%Y-%m"), skill, time.time()),
    )
    return get_agent(aid)


def get_agent(agent_id: str) -> Optional[dict]:
    rows = query("SELECT * FROM agents WHERE id = ?", (agent_id,))
    return rows[0] if rows else None


def active_agents() -> list[dict]:
    return query("SELECT * FROM agents WHERE status = 'active' ORDER BY hired_at")


def all_agents() -> list[dict]:
    return query("SELECT * FROM agents ORDER BY hired_at")


def fire_agent(agent_id: str, reason: str) -> None:
    execute(
        "UPDATE agents SET status='fired', fired_at=?, fired_reason=? WHERE id=?",
        (time.time(), reason, agent_id),
    )
    # Requeue only NOT-yet-started tickets. A task already 'in_progress' is mid
    # LLM call inside execute_task; yanking it would orphan the pending result
    # write and cause a duplicate re-run — let it finish and be reviewed.
    requeue_assigned(agent_id)


def requeue_assigned(agent_id: str) -> None:
    """Send an agent's un-started ('assigned') tickets back to the inbox."""
    execute(
        "UPDATE tasks SET status='inbox', agent_id=NULL, progress=0, updated_at=? "
        "WHERE agent_id=? AND status='assigned'",
        (time.time(), agent_id),
    )


def rescue_orphan_tasks() -> int:
    """Requeue any 'assigned' ticket whose agent is no longer active (fired or
    throttled) so an active agent can pick it up — otherwise it would stall
    forever (the execute query only runs active agents' tickets)."""
    return _execute_rowcount(
        "UPDATE tasks SET status='inbox', agent_id=NULL, progress=0, updated_at=? "
        "WHERE status='assigned' AND (agent_id IS NULL OR agent_id NOT IN "
        "(SELECT id FROM agents WHERE status='active'))",
        (time.time(),))


def add_agent_tokens(agent_id: str, tokens: int) -> None:
    month = time.strftime("%Y-%m")
    agent = get_agent(agent_id)
    if not agent:
        return
    if agent["budget_month"] != month:  # new month: reset meter
        execute("UPDATE agents SET tokens_used=0, budget_month=? WHERE id=?", (month, agent_id))
    execute("UPDATE agents SET tokens_used = tokens_used + ? WHERE id = ?", (tokens, agent_id))


def update_performance(agent_id: str, score: int, failed: bool) -> float:
    """Exponential moving average of task scores; returns the new performance."""
    agent = get_agent(agent_id)
    if not agent:
        return 0.0
    perf = 0.65 * agent["performance"] + 0.35 * score
    done = agent["tasks_done"] + (0 if failed else 1)
    failed_n = agent["tasks_failed"] + (1 if failed else 0)
    execute(
        "UPDATE agents SET performance=?, tasks_done=?, tasks_failed=? WHERE id=?",
        (perf, done, failed_n, agent_id),
    )
    return perf


# ---------------------------------------------------------------- tasks

def create_task(title: str, description: str = "", priority: int = 2,
                project_id: str = "", deps: str = "", status: str = "inbox") -> dict:
    tid = new_id()
    now = time.time()
    execute(
        "INSERT INTO tasks (id, title, description, priority, status, project_id, deps, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, title, description, priority, status, project_id, deps, now, now),
    )
    return get_task(tid)


# ---------------------------------------------------------------- projects (multi-step)

def create_project(goal: str) -> dict:
    pid = new_id()
    now = time.time()
    execute("INSERT INTO projects (id, goal, created, updated) VALUES (?,?,?,?)", (pid, goal, now, now))
    return query("SELECT * FROM projects WHERE id=?", (pid,))[0]


def get_project(pid: str) -> Optional[dict]:
    rows = query("SELECT * FROM projects WHERE id=?", (pid,))
    return rows[0] if rows else None


def active_projects() -> list[dict]:
    return query("SELECT * FROM projects WHERE status='active' ORDER BY created DESC")


def all_projects(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM projects ORDER BY created DESC LIMIT ?", (limit,))


def project_tasks(pid: str) -> list[dict]:
    return query("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at", (pid,))


def update_project(pid: str, **fields) -> None:
    if not fields:
        return
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))


def set_task_status(task_id: str, status: str) -> None:
    execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, time.time(), task_id))


def get_task(task_id: str) -> Optional[dict]:
    rows = query("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return rows[0] if rows else None


def tasks_by_status(status: str) -> list[dict]:
    return query("SELECT * FROM tasks WHERE status=? ORDER BY priority, created_at", (status,))


def all_tasks(limit: int = 200) -> list[dict]:
    return query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))


def update_task(task_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def set_progress(task_id: str, pct: int) -> None:
    """Update a task's live progress (0-100), monotonic while it's running so a
    concurrent read never sees it jump backwards mid-execution."""
    pct = max(0, min(100, int(pct)))
    execute("UPDATE tasks SET progress=?, updated_at=? WHERE id=? AND progress<?",
            (pct, time.time(), task_id, pct))


def finalize_task(task_id: str, agent_id: str, **fields) -> bool:
    """Compare-and-set write for an agent finishing its ticket: only applies if
    the task is still 'in_progress' under this agent. Returns False if it was
    reassigned out from under the agent mid-run (so we discard the stale result
    instead of orphaning it). Guards against the fire-during-execution race."""
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    return _execute_rowcount(
        f"UPDATE tasks SET {cols} WHERE id=? AND agent_id=? AND status='in_progress'",
        (*fields.values(), task_id, agent_id)) > 0


# ---------------------------------------------------------------- chat sessions

def create_session(title: str = "New chat") -> dict:
    sid = new_id()
    now = time.time()
    execute("INSERT INTO chat_sessions (id, title, created, updated) VALUES (?,?,?,?)",
            (sid, title, now, now))
    return query("SELECT * FROM chat_sessions WHERE id=?", (sid,))[0]


def list_sessions() -> list[dict]:
    return query(
        "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count "
        "FROM chat_sessions s ORDER BY s.updated DESC"
    )


def get_session(session_id: str) -> Optional[dict]:
    rows = query("SELECT * FROM chat_sessions WHERE id=?", (session_id,))
    return rows[0] if rows else None


def rename_session(session_id: str, title: str) -> None:
    execute("UPDATE chat_sessions SET title=? WHERE id=?", (title[:120], session_id))


def delete_session(session_id: str) -> None:
    execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


def touch_session(session_id: str) -> None:
    execute("UPDATE chat_sessions SET updated=? WHERE id=?", (time.time(), session_id))


# ---------------------------------------------------------------- chat

def add_message(role: str, content: str, session_id: str = "main") -> int:
    mid = _insert_returning_id(
        "INSERT INTO messages (ts, role, content, session_id) VALUES (?,?,?,?)",
        (time.time(), role, content, session_id))
    touch_session(session_id)
    return mid


def recent_messages(limit: int = 20, session_id: str = "main") -> list[dict]:
    rows = query("SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                 (session_id, limit))
    return list(reversed(rows))


# ---------------------------------------------------------------- memory
# Persistent: typed memories with importance and usage tracking,
# recalled by hybrid scoring (FTS5 keyword rank × importance × recency).

def remember(content: str, kind: str = "note", agent: str = "", importance: float = 1.0) -> int:
    """Store a memory and return its id (existing id if reinforced, 0 if empty)."""
    content = content.strip()
    if not content:
        return 0
    dup = query("SELECT id, importance FROM memories WHERE content = ? LIMIT 1", (content,))
    if dup:  # reinforced, not duplicated
        execute("UPDATE memories SET importance = importance + 0.5, last_used = ? WHERE id = ?",
                (time.time(), dup[0]["id"]))
        return dup[0]["id"]
    now = time.time()
    return _insert_returning_id(
        "INSERT INTO memories (content, kind, agent, importance, created, last_used) VALUES (?,?,?,?,?,?)",
        (content, kind, agent, importance, now, now))


# ---------------------------------------------------------------- approvals (command gate)

def add_approval(agent: str, task_id: str, task_title: str, tool: str,
                 command: str, reason: str, meta: str = "") -> int:
    return _insert_returning_id(
        "INSERT INTO approvals (agent, task_id, task_title, tool, command, reason, meta, created) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (agent, task_id, task_title, tool, command, reason, meta, time.time()))


def pending_approvals() -> list[dict]:
    return query("SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC")


def get_approval(aid: int) -> dict | None:
    rows = query("SELECT * FROM approvals WHERE id = ?", (aid,))
    return rows[0] if rows else None


def resolve_approval(aid: int, status: str, output: str = "") -> None:
    execute("UPDATE approvals SET status=?, output=?, resolved=? WHERE id=?",
            (status, output[:6000], time.time(), aid))


# Common function words that, OR-matched against FTS, match almost any document
# and drown out the real keywords (e.g. "the"/"when" in a long question). Dropping
# them keeps recall — and the graph mediator's coverage signal — precise.
_RECALL_STOP = frozenset("""the and for with from that this what when where which who whom
whose will would can could should have has had are was were you your our its their them they
about into over under not but how why does did done your you're get got make made just like
want wants need needs please thanks okay zax founder""".split())


def recall(text: str, limit: int = 3, kinds: Optional[list[str]] = None,
           agent: str = "") -> list[dict]:
    # FTS5 match queries choke on punctuation; keep alphanumeric words only.
    words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if len(w) > 2]
    # Drop stopwords so common function words can't trigger spurious matches; if
    # that leaves nothing (a query made entirely of stopwords), keep the originals.
    content_words = [w for w in words if w.lower() not in _RECALL_STOP]
    words = content_words or words
    if not words:
        return []
    uniq = list(dict.fromkeys(words[:12]))
    if PG:
        # Postgres full-text: websearch_to_tsquery handles the OR query leniently,
        # ts_rank orders candidates (the Python pass below re-scores by importance).
        tsq = " OR ".join(uniq)
        sql = ("SELECT m.*, ts_rank(to_tsvector('english', m.content), "
               "websearch_to_tsquery('english', ?)) AS _rank FROM memories m "
               "WHERE to_tsvector('english', m.content) @@ websearch_to_tsquery('english', ?)")
        params: list = [tsq, tsq]
        order = " ORDER BY _rank DESC LIMIT ?"
    else:
        sql = ("SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid"
               " WHERE memories_fts MATCH ?")
        params = [" OR ".join(uniq)]
        order = " ORDER BY rank LIMIT ?"
    if kinds:
        sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if agent:
        sql += " AND (m.agent = '' OR m.agent = ?)"
        params.append(agent)
    sql += order
    params.append(max(limit * 4, 12))
    try:
        candidates = query(sql, tuple(params))
    except Exception:
        return []
    now = time.time()
    scored = []
    for pos, m in enumerate(candidates):
        recency = 1.0 if now - m["created"] < 30 * 86400 else 0.6
        scored.append((m["importance"] * (0.85 ** pos) * recency, m))
    scored.sort(key=lambda s: -s[0])
    hits = [m for _, m in scored[:limit]]
    for m in hits:
        execute("UPDATE memories SET uses = uses + 1, last_used = ? WHERE id = ?", (now, m["id"]))
    return hits


def list_memories(kind: str = "", limit: int = 60) -> list[dict]:
    if kind:
        return query("SELECT * FROM memories WHERE kind = ? ORDER BY created DESC LIMIT ?", (kind, limit))
    return query("SELECT * FROM memories ORDER BY created DESC LIMIT ?", (limit,))


def delete_memory(mem_id: int) -> None:
    execute("DELETE FROM memories WHERE id = ?", (mem_id,))


def memory_counts() -> dict:
    rows = query("SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind")
    return {r["kind"]: r["n"] for r in rows}


# ---------------------------------------------------------------- routines

def create_routine(name: str, description: str, interval_minutes: int) -> dict:
    rid = new_id()
    execute(
        "INSERT INTO routines (id, name, description, interval_minutes) VALUES (?,?,?,?)",
        (rid, name, description, interval_minutes),
    )
    return query("SELECT * FROM routines WHERE id=?", (rid,))[0]


def due_routines() -> list[dict]:
    now = time.time()
    return [
        r for r in query("SELECT * FROM routines WHERE enabled=1")
        if now - r["last_run"] >= r["interval_minutes"] * 60
    ]


def touch_routine(routine_id: str) -> None:
    execute("UPDATE routines SET last_run=? WHERE id=?", (time.time(), routine_id))


def all_routines() -> list[dict]:
    return query("SELECT * FROM routines ORDER BY name")


def delete_routine(routine_id: str) -> None:
    execute("DELETE FROM routines WHERE id=?", (routine_id,))


# ---------------------------------------------------------------- settings

def get_setting(key: str, default: str = "") -> str:
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))


# ---------------------------------------------------------------- knowledge graph

def upsert_node(node_id: str, label: str, kind: str, summary: str, source: str) -> None:
    now = time.time()
    execute(
        "INSERT INTO graph_nodes (id, label, kind, summary, source, created, last_seen)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET weight = weight + 1.0, last_seen = excluded.last_seen,"
        " summary = CASE WHEN length(excluded.summary) > length(graph_nodes.summary)"
        "                THEN excluded.summary ELSE graph_nodes.summary END",
        (node_id, label, kind, summary, source, now, now),
    )


def upsert_edge(src: str, tgt: str, relation: str, confidence: str, context: str) -> None:
    execute(
        "INSERT INTO graph_edges (src, tgt, relation, confidence, context, created)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(src, tgt, relation) DO UPDATE SET weight = weight + 1.0,"
        " confidence = CASE WHEN excluded.confidence = 'EXTRACTED' THEN 'EXTRACTED'"
        "                   ELSE graph_edges.confidence END",
        (src, tgt, relation, confidence, context, time.time()),
    )


def graph_nodes() -> list[dict]:
    return query("SELECT * FROM graph_nodes")


def graph_edges() -> list[dict]:
    return query("SELECT * FROM graph_edges")


def graph_node_count() -> int:
    rows = query("SELECT COUNT(*) AS n FROM graph_nodes")
    return rows[0]["n"]


def graph_edge_count() -> int:
    rows = query("SELECT COUNT(*) AS n FROM graph_edges")
    return rows[0]["n"]


def delete_node(node_id: str) -> None:
    execute("DELETE FROM graph_nodes WHERE id=?", (node_id,))
    execute("DELETE FROM graph_edges WHERE src=? OR tgt=?", (node_id, node_id))
    execute("DELETE FROM graph_provenance WHERE node_id=?", (node_id,))


def clear_graph() -> None:
    execute("DELETE FROM graph_nodes")
    execute("DELETE FROM graph_edges")
    execute("DELETE FROM graph_provenance")


# ---------------------------------------------------------------- provenance (graph ↔ source)

def link_provenance(node_id: str, ref_kind: str, ref_id: str) -> None:
    """Record that `node_id` was distilled from a source row (memory/task/message)."""
    execute(
        "INSERT INTO graph_provenance (node_id, ref_kind, ref_id, created) VALUES (?,?,?,?)"
        " ON CONFLICT(node_id, ref_kind, ref_id) DO UPDATE SET weight = weight + 1.0",
        (node_id, ref_kind, str(ref_id), time.time()),
    )


def memory_ids_for_nodes(node_ids: list[str], limit: int = 12) -> list[int]:
    """Memory ids linked to any of these graph nodes, most-linked first.

    This is the mediator's core lookup: given the relevant subgraph, fetch the
    exact memories those nodes point back to."""
    if not node_ids:
        return []
    ph = ",".join("?" * len(node_ids))
    rows = query(
        f"SELECT ref_id, SUM(weight) AS w FROM graph_provenance"
        f" WHERE ref_kind='memory' AND node_id IN ({ph})"
        f" GROUP BY ref_id ORDER BY w DESC LIMIT ?",
        (*node_ids, limit),
    )
    out = []
    for r in rows:
        try:
            out.append(int(r["ref_id"]))
        except (TypeError, ValueError):
            continue
    return out


def memories_by_ids(ids: list[int]) -> list[dict]:
    """Fetch memory rows by id, preserving the order of `ids`."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = query(f"SELECT * FROM memories WHERE id IN ({ph})", tuple(ids))
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]
