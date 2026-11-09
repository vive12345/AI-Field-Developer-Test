"""Token-aware sliding-window rate limiter backed by an on-disk SQLite file.

Model: every admitted request is a row (tenant, tokens, timestamp). A tenant's
usage is the sum of its rows from the last `window_seconds`. Unlike a fixed
window, this has no burst at window boundaries: a token counts for exactly
60 seconds after it was reserved.

Lifecycle of one request:
    try_acquire(estimate) -> reserve estimated tokens, or reject with Retry-After
    adjust(actual)        -> replace the estimate with the provider's real usage
    release()             -> refund the reservation if the request failed

Concurrency: each decision runs in a single `BEGIN IMMEDIATE` transaction,
which takes SQLite's write lock up front. Check-and-reserve is therefore atomic,
both across asyncio tasks (each call runs in a worker thread with its own
connection) and across multiple gateway processes sharing the same file.
"""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT    NOT NULL,
    tokens     INTEGER NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON token_usage (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON token_usage (created_at);
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float = 0.0
    reservation_id: int | None = None


class SlidingWindowRateLimiter:
    def __init__(self, db_path: str, window_seconds: float = 60.0, clock: Callable[[], float] = time.time):
        # Wall-clock time (not monotonic) because timestamps are persisted and
        # shared between processes.
        self._db_path = db_path
        self._window = window_seconds
        self._clock = clock
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        # timeout: wait up to 5s for another writer instead of failing immediately.
        return sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)

    def try_acquire(self, tenant_id: str, tokens: int, limit: int) -> RateLimitDecision:
        now = self._clock()
        window_start = now - self._window

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Eviction: drop every row (any tenant) that has left the window.
                conn.execute("DELETE FROM token_usage WHERE created_at <= ?", (window_start,))

                (used,) = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()

                if used + tokens > limit:
                    retry_after = self._seconds_until_capacity(conn, tenant_id, used + tokens - limit, now)
                    conn.execute("COMMIT")  # keep the eviction
                    return RateLimitDecision(False, limit, max(0, limit - used), retry_after_seconds=retry_after)

                cursor = conn.execute(
                    "INSERT INTO token_usage (tenant_id, tokens, created_at) VALUES (?, ?, ?)",
                    (tenant_id, tokens, now),
                )
                conn.execute("COMMIT")
                return RateLimitDecision(True, limit, limit - used - tokens, reservation_id=cursor.lastrowid)
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def adjust(self, reservation_id: int, actual_tokens: int) -> None:
        """Replace an estimated reservation with the tokens actually consumed."""
        with closing(self._connect()) as conn:
            conn.execute("UPDATE token_usage SET tokens = ? WHERE id = ?", (max(0, actual_tokens), reservation_id))

    def release(self, reservation_id: int) -> None:
        """Refund a reservation (the request never produced a completion)."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM token_usage WHERE id = ?", (reservation_id,))

    def usage(self, tenant_id: str) -> int:
        """Tokens currently counted against the tenant (for tests and diagnostics)."""
        window_start = self._clock() - self._window
        with closing(self._connect()) as conn:
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_id = ? AND created_at > ?",
                (tenant_id, window_start),
            ).fetchone()
        return used

    def _seconds_until_capacity(self, conn: sqlite3.Connection, tenant_id: str, excess: int, now: float) -> float:
        """How long until enough of the oldest reservations expire to free `excess` tokens."""
        freed = 0
        rows = conn.execute(
            "SELECT tokens, created_at FROM token_usage WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)
        )
        for tokens, created_at in rows:
            freed += tokens
            if freed >= excess:
                return max(0.0, created_at + self._window - now)
        return self._window
