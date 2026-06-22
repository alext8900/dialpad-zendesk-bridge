"""Tiny SQLite store so dedup + multi-phase updates survive restarts.
Stdlib only, no extra deps. Volume-mount the db file in docker-compose."""

import os
import sqlite3

DB_PATH = os.environ.get("STATE_DB", "/data/state.db")


def _conn():
    return sqlite3.connect(DB_PATH)


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                   call_id TEXT PRIMARY KEY,
                   ticket_id INTEGER,
                   subject TEXT,
                   recap_done INTEGER DEFAULT 0,
                   enriched INTEGER DEFAULT 0,
                   vm_link_done INTEGER DEFAULT 0,
                   vm_transcript_done INTEGER DEFAULT 0,
                   assigned INTEGER DEFAULT 0
               )"""
        )
        # Upgrade older databases: add any columns missing from earlier versions.
        for col in ("recap_done", "enriched", "vm_link_done",
                    "vm_transcript_done", "assigned"):
            try:
                c.execute(f"ALTER TABLE calls ADD COLUMN {col} INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            c.execute("ALTER TABLE calls ADD COLUMN subject TEXT")
        except sqlite3.OperationalError:
            pass
        # Alias map: any Dialpad call-graph id (call_id / master_call_id /
        # entry_point_call_id / operator_call_id) -> the ONE canonical key for the
        # call. Legs cross-reference each other by different fields, so we union
        # all of them into a single ticket. See resolve_and_link().
        c.execute(
            """CREATE TABLE IF NOT EXISTS aliases (
                   alias TEXT PRIMARY KEY,
                   call_key TEXT NOT NULL
               )"""
        )


def resolve_and_link(ids) -> str:
    """Given all the call-graph ids on an event, return the canonical key they
    belong to, linking every id to it. If several ids already map to different
    keys (legs unified late), merge them — preferring the one that already has a
    ticket — so the whole call collapses to a single ticket."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return None
    with _conn() as c:
        found = []
        for alias in ids:
            row = c.execute(
                "SELECT call_key FROM aliases WHERE alias = ?", (alias,)
            ).fetchone()
            if row and row[0] not in found:
                found.append(row[0])
        if not found:
            canonical = ids[0]
        elif len(found) == 1:
            canonical = found[0]
        else:
            with_ticket = [k for k in found if c.execute(
                "SELECT 1 FROM calls WHERE call_id = ? AND ticket_id IS NOT NULL",
                (k,)).fetchone()]
            canonical = (with_ticket or found)[0]
            for other in found:
                if other != canonical:
                    c.execute("UPDATE aliases SET call_key = ? WHERE call_key = ?",
                              (canonical, other))
        for alias in ids:
            c.execute(
                "INSERT INTO aliases (alias, call_key) VALUES (?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET call_key = excluded.call_key",
                (alias, canonical),
            )
        return canonical


def get_ticket(call_id: str):
    with _conn() as c:
        row = c.execute(
            "SELECT ticket_id FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return row[0] if row else None


def save_ticket(call_id: str, ticket_id: int, subject: str = None):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO calls (call_id, ticket_id, subject) VALUES (?, ?, ?)",
            (call_id, ticket_id, subject),
        )


def get_subject(call_id: str):
    with _conn() as c:
        row = c.execute(
            "SELECT subject FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return row[0] if row else None


def recap_done(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT recap_done FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_recap(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET recap_done = 1 WHERE call_id = ?", (call_id,))


def vm_link_done(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT vm_link_done FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_vm_link(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET vm_link_done = 1 WHERE call_id = ?", (call_id,))


def vm_transcript_done(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT vm_transcript_done FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_vm_transcript(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET vm_transcript_done = 1 WHERE call_id = ?", (call_id,))


def is_assigned(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT assigned FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_assigned(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET assigned = 1 WHERE call_id = ?", (call_id,))


def is_enriched(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT enriched FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_enriched(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET enriched = 1 WHERE call_id = ?", (call_id,))
