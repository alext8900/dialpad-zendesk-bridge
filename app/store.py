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
