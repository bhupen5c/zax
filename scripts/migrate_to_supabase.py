"""One-time migration: copy local data/zax.db (SQLite) → Postgres at $DATABASE_URL.

Usage:
    DATABASE_URL='postgresql://...supabase...' .venv/bin/python scripts/migrate_to_supabase.py

Idempotent (ON CONFLICT DO NOTHING), preserves primary keys — including the
auto-increment ids on events/messages/memories so graph_provenance links stay
valid — then fixes each identity sequence. Run the schema migration first.
"""
import os
import sqlite3
import sys

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = os.environ.get("ZAX_SQLITE", "data/zax.db")

# table -> whether it has a GENERATED-ALWAYS identity 'id' (needs OVERRIDING + setval)
TABLES = {
    "agents": False, "tasks": False, "chat_sessions": False, "settings": False,
    "routines": False, "graph_nodes": False, "graph_edges": False,
    "graph_provenance": False, "events": True, "messages": True, "memories": True,
}


def migrate_table(slite: sqlite3.Connection, pg: psycopg.Connection, table: str, identity: bool) -> int:
    rows = slite.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return 0
    cols = list(rows[0].keys())
    collist = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    override = "OVERRIDING SYSTEM VALUE " if identity else ""
    sql = f"INSERT INTO {table} ({collist}) {override}VALUES ({ph}) ON CONFLICT DO NOTHING"
    data = [tuple(r[c] for c in cols) for r in rows]
    with pg.cursor() as cur:
        cur.executemany(sql, data)
        if identity:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), 1))"
            )
    pg.commit()
    return len(rows)


def main() -> int:
    if not DATABASE_URL:
        print("Set DATABASE_URL to your Supabase Postgres connection string.", file=sys.stderr)
        return 1
    if not os.path.exists(SQLITE_PATH):
        print(f"No SQLite DB at {SQLITE_PATH}", file=sys.stderr)
        return 1
    slite = sqlite3.connect(SQLITE_PATH)
    slite.row_factory = sqlite3.Row
    pg = psycopg.connect(DATABASE_URL)
    total = 0
    for table, identity in TABLES.items():
        try:
            n = migrate_table(slite, pg, table, identity)
            total += n
            print(f"  {table:18} {n:5} rows")
        except Exception as exc:
            print(f"  {table:18} FAILED: {exc}", file=sys.stderr)
    pg.close()
    print(f"Done — {total} rows migrated into Supabase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
