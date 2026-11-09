"""Sliding-window limiter: accounting, eviction, retry-after, persistence and concurrency."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from llm_router.rate_limiter import SlidingWindowRateLimiter

LIMIT = 50_000


def make_limiter(db_path, clock) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(db_path, window_seconds=60, clock=clock)


def row_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]


# --- accounting -----------------------------------------------------------------


def test_admits_until_limit_then_rejects(db_path, clock):
    limiter = make_limiter(db_path, clock)

    assert limiter.try_acquire("acme", 30_000, LIMIT).remaining == 20_000
    assert limiter.try_acquire("acme", 20_000, LIMIT).remaining == 0
    rejected = limiter.try_acquire("acme", 1, LIMIT)

    assert not rejected.allowed
    assert limiter.usage("acme") == 50_000  # rejected requests reserve nothing


def test_tenants_have_independent_budgets(db_path, clock):
    limiter = make_limiter(db_path, clock)
    limiter.try_acquire("acme", LIMIT, LIMIT)

    assert limiter.try_acquire("globex", LIMIT, LIMIT).allowed
    assert not limiter.try_acquire("acme", 1, LIMIT).allowed


def test_adjust_replaces_estimate_with_actual_usage(db_path, clock):
    limiter = make_limiter(db_path, clock)
    decision = limiter.try_acquire("acme", 10_000, LIMIT)

    limiter.adjust(decision.reservation_id, 1_234)

    assert limiter.usage("acme") == 1_234


def test_release_refunds_the_reservation(db_path, clock):
    limiter = make_limiter(db_path, clock)
    decision = limiter.try_acquire("acme", 10_000, LIMIT)

    limiter.release(decision.reservation_id)

    assert limiter.usage("acme") == 0


# --- sliding window and eviction ----------------------------------------------


def test_tokens_count_for_exactly_one_window(db_path, clock):
    limiter = make_limiter(db_path, clock)
    limiter.try_acquire("acme", LIMIT, LIMIT)

    clock.advance(59.999)
    assert not limiter.try_acquire("acme", 1, LIMIT).allowed

    clock.advance(0.001)  # exactly 60s after the reservation
    assert limiter.try_acquire("acme", 1, LIMIT).allowed


def test_window_slides_instead_of_resetting(db_path, clock):
    limiter = make_limiter(db_path, clock)
    limiter.try_acquire("acme", 30_000, LIMIT)  # t=0
    clock.advance(30)
    limiter.try_acquire("acme", 20_000, LIMIT)  # t=30

    clock.advance(31)  # t=61: the first 30k expired, the 20k from t=30 still counts
    assert limiter.try_acquire("acme", 30_000, LIMIT).allowed
    assert not limiter.try_acquire("acme", 1, LIMIT).allowed


def test_expired_rows_are_physically_evicted(db_path, clock):
    limiter = make_limiter(db_path, clock)
    for tenant in ("acme", "globex", "tiny"):
        limiter.try_acquire(tenant, 100, LIMIT)
    assert row_count(db_path) == 3

    clock.advance(61)
    limiter.try_acquire("acme", 100, LIMIT)  # any acquire evicts expired rows of all tenants

    assert row_count(db_path) == 1


def test_retry_after_is_when_enough_capacity_frees_up(db_path, clock):
    limiter = make_limiter(db_path, clock)
    limiter.try_acquire("acme", 20_000, LIMIT)  # t=0, expires at 60
    clock.advance(10)
    limiter.try_acquire("acme", 30_000, LIMIT)  # t=10, expires at 70
    clock.advance(5)  # t=15

    small = limiter.try_acquire("acme", 10_000, LIMIT)  # needs the 20k block to expire
    large = limiter.try_acquire("acme", 25_000, LIMIT)  # needs both blocks to expire

    assert small.retry_after_seconds == 45  # 60 - 15
    assert large.retry_after_seconds == 55  # 70 - 15


# --- durability and concurrency ---------------------------------------------------


def test_state_survives_a_restart(db_path, clock):
    make_limiter(db_path, clock).try_acquire("acme", 40_000, LIMIT)

    restarted = make_limiter(db_path, clock)  # new instance, same on-disk file

    assert restarted.usage("acme") == 40_000
    assert not restarted.try_acquire("acme", 20_000, LIMIT).allowed


def test_concurrent_acquires_never_overshoot_the_limit(db_path, clock):
    limiter = make_limiter(db_path, clock)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _: limiter.try_acquire("acme", 2_000, LIMIT), range(60)))

    assert sum(d.allowed for d in decisions) == 25  # 25 x 2,000 = 50,000
    assert limiter.usage("acme") == LIMIT


def test_retry_after_never_exceeds_the_window_under_contention(db_path):
    # Every request wants the whole budget and all start at once: one wins, and
    # the rest wait for its write lock. Those waiters must read the clock after
    # the winner committed, otherwise Retry-After comes out longer than the window.
    limiter = SlidingWindowRateLimiter(db_path, window_seconds=60)
    start = threading.Barrier(16)

    def contend(_):
        start.wait()
        return limiter.try_acquire("acme", 2_000, 2_000)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(contend, range(16)))

    rejected = [d for d in decisions if not d.allowed]
    assert len(rejected) == 15
    assert max(d.retry_after_seconds for d in rejected) <= 60


def test_two_gateway_instances_sharing_the_file_stay_consistent(db_path, clock):
    # Simulates two gateway processes using the same SQLite database.
    instances = [make_limiter(db_path, clock), make_limiter(db_path, clock)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda i: instances[i % 2].try_acquire("acme", 5_000, LIMIT), range(40)))

    assert sum(d.allowed for d in decisions) == 10
    assert instances[0].usage("acme") == LIMIT
