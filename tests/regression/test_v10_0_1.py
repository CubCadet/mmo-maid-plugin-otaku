"""Regression contract for otaku v10.0.1 — post-audit safety + perf patch.

IMMUTABLE — what shipped at v10.0.1:

REVIEW UPSERT (TOCTOU fix)
- `_upsert_review` MUST collapse INSERT + UPDATE into a single
  `INSERT ... ON CONFLICT (user_id, media_id) DO UPDATE ... RETURNING
  (xmax = 0) AS inserted`. The prior read-then-write left a window where
  two concurrent modal submits for the same (user, media) could both see
  "no existing row" and race the INSERT, with the loser raising
  IntegrityError out of `_handle_review_submit`.
- The return value still distinguishes new-vs-updated (True for fresh
  INSERT, False for UPDATE) so the success message stays correct.

RECOMMEND PEER VECTORS (N+1 fix)
- `_recommend_peer_vectors_batch(ctx, peer_ids)` MUST exist and hydrate
  every peer's rating vector via a single
  `WHERE user_id = ANY($1::TEXT[])` query. Empty `peer_ids` returns `{}`
  without hitting SQL.
- `_recommend_candidates` MUST call the batch helper exactly once per
  invocation (replacing the 50-peer fan-out loop).

ACHIEVEMENT STATS BATCHING (N+1 fix)
- `_ach_load_stats(ctx, user_id)` MUST aggregate every per-user count
  the predicates need in 2 SQL trips: one FILTER aggregate over
  `otaku_user_media` (total/favorites/completed/rated) plus one
  combined `(SELECT COUNT(*) FROM otaku_reviews ...) AS reviews,
  (SELECT COUNT(*) FROM otaku_notifications ...) AS subs`.
- `_ach_stats_scope(ctx, user_id)` is a reentrant context manager. On
  entry it pre-fetches stats and stores them in a per-thread cache; on
  exit it clears them. Inner scopes (e.g. `_check_and_award_achievements`
  nested under `cmd_achievements`) are no-ops so stats load exactly
  ONCE per user-visible call.

  (v10.0.1 used a module-global `_ACH_STATS` dict for the cache. v10.0.2
  swapped that for `threading.local()` after discovering the SDK runs
  multiple dispatcher threads per worker — the dict could leak user A's
  stats into user B's predicates under concurrent /achievements calls.
  The accessor `_ach_stats_current()` replaces direct dict reads.)
- Inside a scope, `_ach_count_rows`, `_ach_count_reviews`,
  `_ach_count_subs` read from the cached aggregate. Outside a scope,
  they fall back to per-call SQL (backwards-compatible for ad-hoc
  callers and tests).

PAGINATION CACHE COVERAGE
- Every paginated `_render_*` MUST pass `cache=True` to `_anilist_query`
  unconditionally (no more `cache=(page == 1)`). The render functions
  covered: `_render_discover`, `_render_trending`,
  `_render_character_popular`, `_render_manga_discover`,
  `_render_premieres`, `_search_by_genre_tag_blend`.
- The cache key already varies by `page` (it's part of the variables
  dict), so each page gets its own 5-minute-TTL entry. The
  ANILIST_CACHE_MAX_ENTRIES cap (256) bounds memory.
"""
from __future__ import annotations

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _modal(custom_id: str, fields: dict, **extra) -> dict:
    return make_event(
        "interaction_create", interaction_type=5,
        custom_id=custom_id,
        modal_values=fields,
        **extra,
    )


# ── _upsert_review TOCTOU collapse ─────────────────────────────────────────


def test_upsert_review_uses_single_on_conflict_query():
    """One combined INSERT...ON CONFLICT DO UPDATE — no separate read-then-write."""
    ctx = MockContext()
    queries: list[tuple[str, list]] = []

    def _q(sql, params=None):
        queries.append((sql, params))
        if "ON CONFLICT" in sql:
            return [{"inserted": True}]
        return []

    ctx.sql.query = _q
    is_new = p._upsert_review(ctx, "u1", 42, title="T", body="B")
    upserts = [q for q in queries if "INSERT INTO otaku_reviews" in q[0]]
    assert len(upserts) == 1
    sql = upserts[0][0]
    assert "ON CONFLICT (user_id, media_id) DO UPDATE" in sql
    assert "RETURNING (xmax = 0)" in sql
    assert "updated_at = NOW()" in sql
    assert is_new is True


def test_upsert_review_returns_false_on_update_path():
    """RETURNING (xmax = 0) — 0 means inserted, non-zero means updated → False."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: (
        [{"inserted": False}] if "ON CONFLICT" in sql else []
    )
    assert p._upsert_review(ctx, "u1", 42, title="t", body="b") is False


def test_upsert_review_does_not_execute_separate_update():
    """Confirm the pre-v10.0.1 INSERT-or-UPDATE split is gone."""
    ctx = MockContext()
    executes: list = []
    ctx.sql.execute = lambda sql, params=None: executes.append(sql)
    ctx.sql.query = lambda sql, params=None: (
        [{"inserted": True}] if "ON CONFLICT" in sql else []
    )
    p._upsert_review(ctx, "u1", 42, title="t", body="b")
    # The entire upsert path goes through ctx.sql.query (RETURNING needs
    # query-shape, not execute-shape). No execute calls expected.
    assert not [s for s in executes if "otaku_reviews" in s]


# ── _recommend_peer_vectors_batch ──────────────────────────────────────────


def test_recommend_peer_vectors_batch_uses_array_param():
    """One query with WHERE user_id = ANY($1::TEXT[]); no per-peer loop."""
    ctx = MockContext()
    queries: list = []

    def _q(sql, params=None):
        queries.append((sql, params))
        return [
            {"user_id": "a", "media_id": 1, "rating": 18},
            {"user_id": "a", "media_id": 2, "rating": 16},
            {"user_id": "b", "media_id": 1, "rating": 14},
        ]

    ctx.sql.query = _q
    out = p._recommend_peer_vectors_batch(ctx, ["a", "b"])
    assert len(queries) == 1
    sql, params = queries[0]
    assert "ANY($1::TEXT[])" in sql
    assert params == [["a", "b"]]
    # rating is /2.0
    assert out == {"a": {1: 9.0, 2: 8.0}, "b": {1: 7.0}}


def test_recommend_peer_vectors_batch_empty_short_circuits():
    """No SQL trip when there are no peers."""
    ctx = MockContext()
    called = []
    ctx.sql.query = lambda sql, params=None: called.append(sql) or []
    out = p._recommend_peer_vectors_batch(ctx, [])
    assert out == {}
    assert called == []


def test_recommend_candidates_calls_batch_exactly_once():
    """The N+1 loop must be replaced by a single batch fetch."""
    ctx = MockContext()
    batch_calls = []
    target_vec = {1: 9.0, 2: 8.0, 3: 7.0}

    def _q(sql, params=None):
        # tracked_ids
        if (
            "SELECT media_id FROM otaku_user_media WHERE user_id = $1" in sql
            and "rating" not in sql
            and "is_favorite" not in sql
            and "status" not in sql
        ):
            return []
        # peer_ids
        if "SELECT DISTINCT user_id" in sql:
            return [{"user_id": "p1"}, {"user_id": "p2"}]
        # batch peer vectors
        if "ANY($1::TEXT[])" in sql:
            batch_calls.append(params)
            return [
                {"user_id": "p1", "media_id": 1, "rating": 18},
                {"user_id": "p1", "media_id": 2, "rating": 16},
                {"user_id": "p1", "media_id": 3, "rating": 14},
                {"user_id": "p2", "media_id": 1, "rating": 18},
                {"user_id": "p2", "media_id": 2, "rating": 16},
                {"user_id": "p2", "media_id": 3, "rating": 14},
            ]
        return []

    ctx.sql.query = _q
    p._recommend_candidates(ctx, "target", target_vec)
    assert len(batch_calls) == 1


# ── _ach_load_stats + _ach_stats_scope ─────────────────────────────────────


def test_ach_load_stats_uses_filter_aggregate():
    """Single FILTER aggregate over otaku_user_media; one paired subquery for reviews+subs."""
    ctx = MockContext()
    sqls: list = []

    def _q(sql, params=None):
        sqls.append(sql)
        if "FROM otaku_user_media" in sql and "FILTER" in sql:
            return [{"total": 12, "favorites": 1, "completed": 10, "rated": 5}]
        if "FROM otaku_reviews WHERE user_id = $1" in sql and "FROM otaku_notifications" in sql:
            return [{"reviews": 2, "subs": 3}]
        return []

    ctx.sql.query = _q
    stats = p._ach_load_stats(ctx, "u")
    assert stats == {
        "total": 12, "favorites": 1, "completed": 10, "rated": 5,
        "reviews": 2, "subs": 3,
    }
    # Exactly 2 SQL trips for all six aggregates.
    assert len(sqls) == 2


def test_ach_load_stats_zero_for_empty_user_id():
    """Defensive: no SQL when user_id is falsy."""
    ctx = MockContext()
    called = []
    ctx.sql.query = lambda sql, params=None: called.append(sql) or []
    stats = p._ach_load_stats(ctx, "")
    assert stats == {"total": 0, "favorites": 0, "completed": 0, "rated": 0,
                     "reviews": 0, "subs": 0}
    assert called == []


# regression-fix (v10.0.2): direct `_ACH_STATS["current"]` reads replaced
# with the `_ach_stats_current()` accessor — the underlying cache is now
# `threading.local()` rather than a module-global dict, but the semantics
# (None outside scope, dict inside scope) are unchanged.
def test_ach_stats_scope_caches_within_block():
    """Inside the scope, `_ach_count_rows` reads from cache; outside it hits SQL."""
    ctx = MockContext()
    sql_calls: list = []

    def _q(sql, params=None):
        sql_calls.append(sql)
        if "FROM otaku_user_media" in sql and "FILTER" in sql:
            return [{"total": 7, "favorites": 0, "completed": 5, "rated": 0}]
        if "FROM otaku_reviews WHERE user_id = $1" in sql and "FROM otaku_notifications" in sql:
            return [{"reviews": 0, "subs": 0}]
        return [{"n": 0}]

    ctx.sql.query = _q
    with p._ach_stats_scope(ctx, "u"):
        # In-scope: predicates consult cache; total 2 SQL trips so far.
        assert p._ach_count_rows(ctx, "u") == 7
        assert p._ach_count_rows(ctx, "u", "AND status = 'completed'") == 5
        assert len(sql_calls) == 2

    # Out of scope: cache cleared; the next _ach_count_rows would hit SQL.
    assert p._ach_stats_current() is None


# regression-fix (v10.0.2): see comment on test_ach_stats_scope_caches_within_block.
def test_ach_stats_scope_is_reentrant():
    """Nested scope must not re-fetch; outer scope owns the cache lifetime."""
    ctx = MockContext()
    load_count = [0]

    def _q(sql, params=None):
        if "FROM otaku_user_media" in sql and "FILTER" in sql:
            load_count[0] += 1
            return [{"total": 1, "favorites": 0, "completed": 0, "rated": 0}]
        if "FROM otaku_reviews WHERE user_id = $1" in sql and "FROM otaku_notifications" in sql:
            return [{"reviews": 0, "subs": 0}]
        return []

    ctx.sql.query = _q
    with p._ach_stats_scope(ctx, "u"):
        with p._ach_stats_scope(ctx, "u"):
            assert p._ach_stats_current() is not None
        # Inner exit did NOT clear — outer is still active.
        assert p._ach_stats_current() is not None
    # Outer exit clears.
    assert p._ach_stats_current() is None
    # Aggregate loaded exactly once across the nested scopes.
    assert load_count[0] == 1


# regression-fix (v10.0.2): see comment on test_ach_stats_scope_caches_within_block.
def test_ach_count_rows_falls_back_to_sql_outside_scope():
    """Tests + ad-hoc callers that don't enter a scope still get correct counts."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*) AS n FROM otaku_user_media" in sql and "FILTER" not in sql:
            return [{"n": 42}]
        return []

    ctx.sql.query = _q
    assert p._ach_stats_current() is None  # no scope active
    assert p._ach_count_rows(ctx, "u") == 42


# ── Pagination cache coverage ──────────────────────────────────────────────


def _seen_cache_per_page(monkeypatch, render_call, *, n_pages: int = 3) -> dict:
    """Render `n_pages` pages and return {page: cache_flag} the call site passed."""
    seen: dict = {}

    def _spy(_ctx, query, variables=None, cache=False):
        page = (variables or {}).get("page")
        if page is not None:
            seen[page] = cache
        # Return whatever shape the caller expects from a successful response.
        return {"Page": {"pageInfo": {"hasNextPage": True},
                         "media": [], "characters": []}}

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    for page in range(1, n_pages + 1):
        render_call(ctx, page)
    return seen


def test_discover_caches_every_page(monkeypatch):
    seen = _seen_cache_per_page(
        monkeypatch,
        lambda c, page: p._render_discover(c, "action", "popular", page=page, deferred=True),
    )
    assert all(v is True for v in seen.values()), seen


def test_trending_caches_every_page(monkeypatch):
    seen = _seen_cache_per_page(
        monkeypatch,
        lambda c, page: p._render_trending(c, page=page, deferred=True),
    )
    assert all(v is True for v in seen.values()), seen


def test_character_popular_caches_every_page(monkeypatch):
    seen = _seen_cache_per_page(
        monkeypatch,
        lambda c, page: p._render_character_popular(c, page=page, deferred=True),
    )
    assert all(v is True for v in seen.values()), seen


def test_manga_discover_caches_every_page(monkeypatch):
    seen = _seen_cache_per_page(
        monkeypatch,
        lambda c, page: p._render_manga_discover(c, "action", "popular", page=page, deferred=True),
    )
    assert all(v is True for v in seen.values()), seen


def test_premieres_caches_every_page(monkeypatch):
    seen = _seen_cache_per_page(
        monkeypatch,
        lambda c, page: p._render_premieres(c, "spring", 2026, page=page, deferred=True),
    )
    assert all(v is True for v in seen.values()), seen


def test_genre_tag_blend_caches_every_page(monkeypatch):
    """v9.0 /find + v8.2 /mood share `_search_by_genre_tag_blend`."""
    seen: dict = {}

    def _spy(_ctx, query, variables=None, cache=False):
        page = (variables or {}).get("page")
        if page is not None:
            seen[page] = cache
        return {"Page": {"pageInfo": {"hasNextPage": False}, "media": [{"id": 1}]}}

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p._search_by_genre_tag_blend(ctx, ["Action"], ["Mecha"], 1)
    p._search_by_genre_tag_blend(ctx, ["Action"], ["Mecha"], 2)
    assert all(v is True for v in seen.values()), seen
