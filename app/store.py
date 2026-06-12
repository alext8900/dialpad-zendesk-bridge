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
                   recap_done INTEGER DEFAULT 0,
                   enriched INTEGER DEFAULT 0
               )"""
        )


def get_ticket(call_id: str):
    with _conn() as c:
        row = c.execute(
            "SELECT ticket_id FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return row[0] if row else None


def save_ticket(call_id: str, ticket_id: int):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO calls (call_id, ticket_id) VALUES (?, ?)",
            (call_id, ticket_id),
        )


def recap_done(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT recap_done FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_recap(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET recap_done = 1 WHERE call_id = ?", (call_id,))


def is_enriched(call_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT enriched FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return bool(row and row[0])


def mark_enriched(call_id: str):
    with _conn() as c:
        c.execute("UPDATE calls SET enriched = 1 WHERE call_id = ?", (call_id,))
