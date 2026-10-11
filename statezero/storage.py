import asyncio
import fcntl
import json
import os
import sqlite3
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from .security import UserError


class Store:
    """Durable single-writer state. Production calls run on AsyncStore's worker."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink():
            raise UserError("The database must not be a symbolic link.")
        self.lock_fd = os.open(
            str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise UserError("Another bot process already owns this database.") from None
        except BaseException:
            os.close(self.lock_fd)
            raise
        try:
            self.db = sqlite3.connect(self.path, timeout=2)
            os.chmod(self.path, 0o600)
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise UserError("This database needs a newer bot version.")
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket ON tickets(user_id) WHERE status='open';
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket ON tickets(user_id) WHERE status IN ('open','closing');
                CREATE INDEX IF NOT EXISTS tickets_created ON tickets(created);
                CREATE INDEX IF NOT EXISTS tickets_owner_created ON tickets(user_id,created);
                CREATE TABLE IF NOT EXISTS ticket_requests (
                    request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    category TEXT NOT NULL, parent_id INTEGER NOT NULL,
                    support_roles TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', channel_id INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_request ON ticket_requests(user_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS requests_status ON ticket_requests(status);
                CREATE INDEX IF NOT EXISTS requests_created ON ticket_requests(created);
                CREATE INDEX IF NOT EXISTS requests_owner_created ON ticket_requests(user_id,created);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, actor INTEGER NOT NULL,
                    action TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS audit_created ON audit(created);
                PRAGMA user_version=2;
            """)
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise UserError(
                    "Database integrity check failed; restore a verified backup."
                )
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self.lock_fd)
            raise

    def setting(self, key: str, default=""):
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
        row = self.db.execute(
            "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
        ).fetchone()
        return dict(row) if row else None

    def tickets(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM tickets WHERE status IN ('open','closing')"
            )
        ]

    def open_ticket(self, user_id: int):
        row = self.db.execute(
            "SELECT * FROM tickets WHERE user_id=? AND status IN ('open','closing')",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def add_ticket(self, channel_id: int, user_id: int, category: str):
        with self.db:
            self.db.execute(
                "INSERT INTO tickets(channel_id,user_id,category,created) VALUES (?,?,?,?)",
                (channel_id, user_id, category, time.time()),
            )

    def count_open_tickets(self):
        return (
            self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status IN ('open','closing')"
            ).fetchone()[0]
            + self.db.execute(
                "SELECT COUNT(*) FROM ticket_requests WHERE status='pending'"
            ).fetchone()[0]
        )

    def count_recent_tickets(self, since: float):
        return (
            self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE created>=?", (since,)
            ).fetchone()[0]
            + self.db.execute(
                "SELECT COUNT(*) FROM ticket_requests WHERE created>=? AND status!='completed'",
                (since,),
            ).fetchone()[0]
        )

    def last_ticket_created(self, user_id: int):
        return self.db.execute(
            "SELECT COALESCE(MAX(created),0) FROM (SELECT created FROM tickets WHERE user_id=? UNION ALL SELECT created FROM ticket_requests WHERE user_id=?)",
            (user_id, user_id),
        ).fetchone()[0]

    def close_ticket(self, channel_id: int):
        with self.db:
            self.db.execute(
                "UPDATE tickets SET status='closed' WHERE channel_id=?", (channel_id,)
            )

    def begin_close(self, channel_id: int):
        with self.db:
            self.db.execute(
                "UPDATE tickets SET status='closing' WHERE channel_id=? AND status='open'",
                (channel_id,),
            )

    def request(self, request_id: str):
        row = self.db.execute(
            "SELECT * FROM ticket_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def requests(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM ticket_requests WHERE status='pending'"
            )
        ]

    def reserve_ticket(
        self, request_id, user_id, category, parent_id, support_roles, limits
    ):
        with self.db:
            if self.open_ticket(user_id):
                raise UserError("You already have an active ticket.")
            if self.db.execute(
                "SELECT 1 FROM ticket_requests WHERE user_id=? AND status='pending'",
                (user_id,),
            ).fetchone():
                raise UserError(
                    "A previous ticket request is awaiting recovery. Contact support staff."
                )
            now = time.time()
            if self.last_ticket_created(user_id) > now - limits.ticket_reopen_seconds:
                raise UserError("Please wait before opening another ticket.")
            if self.count_open_tickets() >= limits.max_open_tickets:
                raise UserError(
                    "The support queue is full. Please contact staff or try later."
                )
            if self.count_recent_tickets(now - 3600) >= limits.max_tickets_per_hour:
                raise UserError(
                    "Ticket creation is temporarily limited. Please try later."
                )
            self.db.execute(
                "INSERT INTO ticket_requests(request_id,user_id,category,parent_id,support_roles,created) VALUES (?,?,?,?,?,?)",
                (
                    request_id,
                    user_id,
                    category,
                    parent_id,
                    json.dumps(support_roles),
                    now,
                ),
            )
        return self.request(request_id)

    def complete_request(self, request_id, channel_id, *, closed=False):
        with self.db:
            request = self.request(request_id)
            if not request or request["status"] == "completed":
                raise UserError("This request is already resolved or missing.")
            self.db.execute(
                "INSERT INTO tickets(channel_id,user_id,category,status,created) VALUES (?,?,?,?,?)",
                (
                    channel_id,
                    request["user_id"],
                    request["category"],
                    "closed" if closed else "open",
                    request["created"],
                ),
            )
            self.db.execute(
                "UPDATE ticket_requests SET status='completed',channel_id=? WHERE request_id=?",
                (channel_id, request_id),
            )

    def release_request(self, request_id):
        with self.db:
            self.db.execute(
                "UPDATE ticket_requests SET status='released' WHERE request_id=? AND status='pending'",
                (request_id,),
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

    def health(self):
        return {
            "active_tickets": self.count_open_tickets(),
            "pending_requests": len(self.requests()),
        }

    def close(self):
        self.db.close()
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        os.close(self.lock_fd)


class AsyncStore:
    """A bounded, single-worker database executor; never blocks the gateway loop."""

    METHODS = {
        "setting",
        "set_setting",
        "ticket",
        "tickets",
        "open_ticket",
        "add_ticket",
        "count_open_tickets",
        "count_recent_tickets",
        "last_ticket_created",
        "close_ticket",
        "begin_close",
        "request",
        "requests",
        "reserve_ticket",
        "complete_request",
        "release_request",
        "audit",
        "health",
    }

    def __init__(self, path: str | Path, capacity=64):
        self.path = Path(path)
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="statezero-db"
        )
        self.initialized = self.executor.submit(Store, self.path)
        self.capacity, self.pending, self.closed = capacity, 0, False
        self.latencies = deque(maxlen=500)

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AttributeError(name)

        async def call(*args, **kwargs):
            if self.closed:
                raise UserError("Database is shutting down.")
            if self.pending >= self.capacity:
                raise UserError("Storage is busy. Please try again shortly.")
            self.pending += 1
            started = time.monotonic()
            future = asyncio.get_running_loop().run_in_executor(
                self.executor,
                lambda: getattr(self.initialized.result(), name)(*args, **kwargs),
            )

            def finished(result):
                self.pending -= 1
                self.latencies.append(time.monotonic() - started)
                if not result.cancelled():
                    result.exception()

            future.add_done_callback(finished)
            return await asyncio.shield(future)

        return call

    def _finish(self):
        self.initialized.result().close()

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await asyncio.shield(
                    asyncio.get_running_loop().run_in_executor(
                        self.executor, self._finish
                    )
                )
            finally:
                self.executor.shutdown(wait=False)

    def shutdown(self):
        """Synchronous cleanup only after discord.Client.run has stopped its loop."""
        if not self.closed:
            self.closed = True
            try:
                self.executor.submit(self._finish).result()
            finally:
                self.executor.shutdown(wait=True)


def backup_database(source: Path, destination: Path):
    """Consistent SQLite backup, including committed WAL data, without taking ownership."""
    if source.is_symlink():
        raise UserError("Backup source must not be a symbolic link.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.close(fd)
    try:
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as original:
            with closing(sqlite3.connect(destination)) as snapshot:
                original.backup(snapshot, pages=128, sleep=0.1)
                if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise UserError("Backup integrity check failed.")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
