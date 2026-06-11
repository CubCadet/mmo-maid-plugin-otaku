"""Regression contract for otaku v10.0.2 — concurrency fix.

IMMUTABLE — what shipped at v10.0.2:

ACHIEVEMENT STATS THREAD-LOCAL CACHE
- v10.0.1 cached achievement aggregates in a module-global dict
  (`_ACH_STATS = {"current": None}`) to memoize across the predicate
  loop in `_check_and_award_achievements` + `cmd_achievements`. That
  worked for sequential calls but raced under the SDK's default
  4-thread dispatcher: thread A would set the cache mid-handler;
  thread B (on a different user) would see it already set, mark
  itself as a non-owner reentrant scope, and read user A's stats
  into user B's predicates → user B gets user A's achievements
  awarded.
- v10.0.2 swaps the dict for `threading.local()` (`_ach_stats_tls`).
  Each dispatcher thread now has its own independent `current`
  attribute. Two concurrent `/achievements` calls on different threads
  see two independent caches and zero cross-thread leak.
- New accessors `_ach_stats_current() -> dict | None` and
  `_ach_stats_set(stats)` replace direct dict access. Predicates
  (`_ach_count_rows`, `_ach_count_reviews`, `_ach_count_subs`) and the
  context manager (`_ach_stats_scope`) read/write via these accessors;
  external code (tests, future integrations) should too.
- Reentrant semantics on a single thread are preserved verbatim.
"""
from __future__ import annotations

import threading

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── Single-thread reentrancy preserved (sanity check) ──────────────────────


def test_scope_clears_on_exit_thread_local():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: (
        [{"total": 1, "favorites": 0, "completed": 0, "rated": 0}]
        if "FROM otaku_user_media" in sql and "FILTER" in sql
        else [{"reviews": 0, "subs": 0}]
        if "FROM otaku_reviews WHERE user_id = %s" in sql and "FROM otaku_notifications" in sql
        else []
    )
    assert p._ach_stats_current() is None
    with p._ach_stats_scope(ctx, "u"):
        assert p._ach_stats_current() is not None
    assert p._ach_stats_current() is None


# ── Cross-thread isolation (the v10.0.1 bug) ───────────────────────────────


def test_scope_cache_is_thread_local():
    """Two threads with overlapping scopes must NOT share stats."""
    ctx = MockContext()
    barrier = threading.Barrier(2)
    captured: dict[str, object] = {}

    def _query_for(user_total):
        def _q(sql, params=None):
            if "FROM otaku_user_media" in sql and "FILTER" in sql:
                return [{"total": user_total, "favorites": 0,
                         "completed": user_total, "rated": 0}]
            if "FROM otaku_reviews WHERE user_id = %s" in sql and "FROM otaku_notifications" in sql:
                return [{"reviews": 0, "subs": 0}]
            return []
        return _q

    def _thread_a():
        ctx_a = MockContext()
        ctx_a.sql.query = _query_for(11)
        with p._ach_stats_scope(ctx_a, "user_a"):
            barrier.wait()  # let B enter its scope while A is still in
            # While B is also inside its scope, A must still see its own stats.
            captured["a_total_inside"] = p._ach_stats_current()["total"]
            barrier.wait()  # let B read its own value before A exits
        captured["a_after_exit"] = p._ach_stats_current()

    def _thread_b():
        ctx_b = MockContext()
        ctx_b.sql.query = _query_for(22)
        barrier.wait()  # sync with A's "inside scope" point
        with p._ach_stats_scope(ctx_b, "user_b"):
            captured["b_total_inside"] = p._ach_stats_current()["total"]
            barrier.wait()  # release A so it can read + exit
        captured["b_after_exit"] = p._ach_stats_current()

    # MockContext isn't used directly here — each thread builds its own.
    del ctx

    t_a = threading.Thread(target=_thread_a)
    t_b = threading.Thread(target=_thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert captured["a_total_inside"] == 11, "thread A saw thread B's stats"
    assert captured["b_total_inside"] == 22, "thread B saw thread A's stats"
    assert captured["a_after_exit"] is None
    assert captured["b_after_exit"] is None


def test_outer_thread_cache_invisible_to_new_thread():
    """A scope on the main thread must not be visible to a freshly-spawned
    thread — `threading.local()` attributes are per-thread by definition."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: (
        [{"total": 99, "favorites": 0, "completed": 0, "rated": 0}]
        if "FROM otaku_user_media" in sql and "FILTER" in sql
        else [{"reviews": 0, "subs": 0}]
        if "FROM otaku_reviews WHERE user_id = %s" in sql and "FROM otaku_notifications" in sql
        else []
    )
    seen_from_child: list = []

    def _child():
        seen_from_child.append(p._ach_stats_current())

    with p._ach_stats_scope(ctx, "u"):
        # Main thread sees the cache.
        assert p._ach_stats_current() is not None
        # A fresh thread does NOT.
        t = threading.Thread(target=_child)
        t.start()
        t.join(timeout=5)
        assert seen_from_child == [None]


# ── Accessor API contract ──────────────────────────────────────────────────


def test_ach_stats_current_returns_none_initially():
    """The accessor returns None when no scope is active on this thread."""
    # Guard: if a prior test leaked state, fail loudly.
    assert p._ach_stats_current() is None


def test_ach_stats_set_overrides_current():
    """`_ach_stats_set` writes the per-thread cache; `_ach_stats_current`
    reads it back. Used by `_ach_stats_scope` internals; also exposes a
    seam for advanced tests / future tooling."""
    try:
        sentinel = {"total": 7, "favorites": 0, "completed": 0,
                    "rated": 0, "reviews": 0, "subs": 0}
        p._ach_stats_set(sentinel)
        assert p._ach_stats_current() is sentinel
    finally:
        # Always restore None so later tests aren't poisoned.
        p._ach_stats_set(None)
    assert p._ach_stats_current() is None
