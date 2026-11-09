"""Reserve rolling token budgets atomically across processes sharing one SQLite file."""

import asyncio
import hashlib
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class QuotaExceeded(Exception):
    """Return the earliest useful retry without exposing another tenant's state."""

    def __init__(self, retry_after: int):
        """Store only the public retry interval."""
        self.retry_after = retry_after
        super().__init__("Token budget exceeded")


@dataclass(frozen=True)
class Reservation:
    """Identify one admitted request and its conservative maximum charge."""

    id: str
    tokens: int


class TokenLimiter:
    """Use short write transactions and keep all provider I/O outside database locks."""

    def __init__(self, path: str | Path, limit=50_000, window=60.0, clock=time.time):
        """Persist epoch-based reservations, never raw API keys or prompt text."""
        if str(path) == ":memory:" or limit <= 0 or window <= 0:
            raise ValueError("Use an on-disk database and positive limits")
        self.path = str(path)
        self.limit = limit
        self.window = window
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, created REAL NOT NULL,
                    tokens INTEGER NOT NULL CHECK(tokens >= 0), settled INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tenant_window ON reservations(tenant, created);
                CREATE TABLE IF NOT EXISTS clock (
                    id INTEGER PRIMARY KEY CHECK(id = 1), value REAL NOT NULL
                );
                INSERT OR IGNORE INTO clock VALUES (1, 0);
            """)

    def _connect(self):
        """Give each thread its own connection with a bounded busy wait."""
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    def _reserve(self, tenant: str, tokens: int) -> Reservation:
        """Serialize expiration, summation, and admission in one transaction."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens <= self.limit:
            raise ValueError("Reservation exceeds the configured token budget")
        key = hashlib.sha256(tenant.encode()).hexdigest()
        database = self._connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            # Clamp backward clock changes rather than prematurely expiring persisted usage
            now = max(
                self.clock(), database.execute("SELECT value FROM clock WHERE id=1").fetchone()[0]
            )
            database.execute("UPDATE clock SET value=? WHERE id=1", (now,))
            database.execute("DELETE FROM reservations WHERE created <= ?", (now - self.window,))
            rows = database.execute(
                "SELECT created, tokens FROM reservations WHERE tenant=? ORDER BY created", (key,)
            ).fetchall()
            excess = sum(row[1] for row in rows) + tokens - self.limit
            if excess > 0:
                released = 0
                retry = 1
                for created, cost in rows:
                    released += cost
                    retry = max(1, math.ceil(created + self.window - now))
                    if released >= excess:
                        break
                database.commit()
                raise QuotaExceeded(retry)
            reservation = Reservation(uuid4().hex, tokens)
            database.execute(
                "INSERT INTO reservations(id,tenant,created,tokens) VALUES(?,?,?,?)",
                (reservation.id, key, now, tokens),
            )
            database.commit()
            return reservation
        except BaseException:
            if database.in_transaction:
                database.rollback()
            raise
        finally:
            database.close()

    async def reserve(self, tenant: str, tokens: int) -> Reservation:
        """Keep SQLite busy waits off the async event loop."""
        return await asyncio.to_thread(self._reserve, tenant, tokens)

    def _settle(self, reservation: Reservation, actual: int) -> None:
        """Reduce one charge only when usage is known and inside the reserved bound."""
        if (
            isinstance(actual, bool)
            or not isinstance(actual, int)
            or not 0 <= actual <= reservation.tokens
        ):
            raise ValueError("Usage is outside the reservation")
        with closing(self._connect()) as database:
            database.execute(
                "UPDATE reservations SET tokens=?, settled=1 WHERE id=? AND settled=0",
                (actual, reservation.id),
            )

    async def settle(self, reservation: Reservation, actual: int) -> None:
        """Make settlement idempotent across duplicate completion callbacks."""
        await asyncio.to_thread(self._settle, reservation, actual)
