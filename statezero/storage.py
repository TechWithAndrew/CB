import json
import os
import sqlite3
import time
from pathlib import Path


class Store:
    """Small durable store; one bot process per database. Never stores message bodies or tokens."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=0.25)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket ON tickets(user_id) WHERE status='open';
            CREATE INDEX IF NOT EXISTS tickets_created ON tickets(created);
            CREATE INDEX IF NOT EXISTS tickets_owner_created ON tickets(user_id,created);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, actor INTEGER NOT NULL,
                action TEXT NOT NULL, details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_created ON audit(created);
        """)

    def setting(self, key: str, default="") -> str:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value)
            )

    def ticket(self, channel_id: int):
        return self.db.execute(
            "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
        ).fetchone()

    def open_ticket(self, user_id: int):
        return self.db.execute(
            "SELECT * FROM tickets WHERE user_id=? AND status='open'", (user_id,)
        ).fetchone()

    def add_ticket(self, channel_id: int, user_id: int, category: str):
        with self.db:
            self.db.execute(
                "INSERT INTO tickets(channel_id,user_id,category,created) VALUES (?,?,?,?)",
                (channel_id, user_id, category, time.time()),
            )

    def count_open_tickets(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='open'"
        ).fetchone()[0]

    def count_recent_tickets(self, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM tickets WHERE created>=?", (since,)
        ).fetchone()[0]

    def last_ticket_created(self, user_id: int) -> float:
        return self.db.execute(
            "SELECT COALESCE(MAX(created),0) FROM tickets WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    def close_ticket(self, channel_id: int):
        with self.db:
            self.db.execute(
                "UPDATE tickets SET status='closed' WHERE channel_id=?", (channel_id,)
            )

    def audit(self, actor: int, action: str, **details):
        with self.db:
            self.db.execute(
                "INSERT INTO audit(created,actor,action,details) VALUES (?,?,?,?)",
                (time.time(), actor, action, json.dumps(details)),
            )
            self.db.execute(
                "DELETE FROM audit WHERE created < ?", (time.time() - 90 * 86400,)
            )

    def close(self):
        self.db.close()
