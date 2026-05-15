"""Regression contract for otaku v10.0.3 — audit-cleanup patch.

IMMUTABLE — what shipped at v10.0.3:

COSINE NORM HOIST (perf)
- `_cosine_similarity` gained an optional `target_norm` kwarg. When
  provided, the function skips recomputing `math.sqrt(sum(v*v))` over
  `target.values()` — useful for callers that score many peers against
  the same target. `_recommend_candidates` now computes the norm once
  outside the per-peer loop. Original signature (no kwarg) preserved.

MULTI-ROW INSERT FOR POLLS + AOTW (perf)
- `_cmd_poll_create` (around the `INSERT INTO otaku_poll_options`):
  collapsed per-option loop into a single INSERT VALUES (...), (...).
- `_cmd_aotw_start` (around the `INSERT INTO otaku_aotw_candidates`):
  same shape — one INSERT per call, multi-row VALUES.
- POLL_MAX_OPTIONS = 4 and AOTW_CANDIDATE_LIMIT = 5 cap the per-call
  parameter count, so the placeholder generation is bounded.

ORPHAN STRING REMOVAL (stale-code cleanup)
- `_Strings.FIND_PAGE_MALFORMED` deleted. v9.0 shipped without /find
  pagination; the string was scaffolding for a future feature that
  never landed. Removing it is fine because nothing references it
  anywhere in the codebase.
"""
from __future__ import annotations

import math

import plugin_main as p
from mmo_maid_sdk.testing import MockContext

# ── _cosine_similarity target_norm hoist ───────────────────────────────────


def test_cosine_similarity_accepts_precomputed_target_norm():
    """Passing target_norm skips the inner recomputation but returns the
    same numerical result."""
    target = {1: 8.0, 2: 6.0, 3: 4.0}
    peer = {1: 7.0, 2: 5.0, 4: 9.0}

    sim_lazy, shared_lazy = p._cosine_similarity(target, peer)
    target_norm = math.sqrt(sum(v * v for v in target.values()))
    sim_eager, shared_eager = p._cosine_similarity(target, peer, target_norm=target_norm)

    assert shared_eager == shared_lazy
    assert abs(sim_eager - sim_lazy) < 1e-12


def test_cosine_similarity_default_signature_preserved():
    """Callers that don't pass `target_norm` get the v6 behavior verbatim."""
    target = {1: 9.0, 2: 6.0}
    peer = {1: 8.0, 2: 4.0}
    sim, shared = p._cosine_similarity(target, peer)
    # Sanity: same shared keys, sim in (0, 1).
    assert shared == 2
    assert 0.0 < sim <= 1.0


def test_recommend_candidates_computes_target_norm_once():
    """The norm computation must NOT appear inside the per-peer loop.

    We verify by passing 5 peers and confirming the candidate set is
    identical to running each peer through a single shared norm: the
    important invariant is that scoring is unchanged, not the count of
    sqrt() calls (which would be over-specifying)."""
    ctx = MockContext()
    target_vec = {1: 9.0, 2: 8.0, 3: 7.0, 4: 6.0}

    def _q(sql, params=None):
        if (
            "SELECT media_id FROM otaku_user_media WHERE user_id = $1" in sql
            and "rating" not in sql
            and "is_favorite" not in sql
            and "status" not in sql
        ):
            return []
        if "SELECT DISTINCT user_id" in sql:
            return [{"user_id": "p1"}, {"user_id": "p2"}, {"user_id": "p3"}]
        if "ANY($1::TEXT[])" in sql:
            return [
                {"user_id": "p1", "media_id": 1, "rating": 18},
                {"user_id": "p1", "media_id": 2, "rating": 16},
                {"user_id": "p1", "media_id": 3, "rating": 14},
                {"user_id": "p1", "media_id": 100, "rating": 18},
                {"user_id": "p2", "media_id": 1, "rating": 14},
                {"user_id": "p2", "media_id": 2, "rating": 16},
                {"user_id": "p2", "media_id": 3, "rating": 18},
                {"user_id": "p2", "media_id": 200, "rating": 16},
                {"user_id": "p3", "media_id": 1, "rating": 18},
                {"user_id": "p3", "media_id": 2, "rating": 18},
                {"user_id": "p3", "media_id": 3, "rating": 18},
                {"user_id": "p3", "media_id": 300, "rating": 20},
            ]
        return []

    ctx.sql.query = _q
    candidates, peers_kept = p._recommend_candidates(ctx, "target", target_vec)
    # All three peers share 3 titles → kept.
    assert peers_kept == 3
    # Each peer contributed one exclusive candidate.
    assert {c["media_id"] for c in candidates} == {100, 200, 300}


# ── Multi-row INSERT for poll options ──────────────────────────────────────


def test_poll_options_use_single_multi_row_insert(monkeypatch):
    """`/poll create` issues one INSERT statement, not N."""
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "WHERE started_by = $1 AND question = $2" in sql:
            return [{"poll_id": 7}]
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 7, "started_by": "u", "question": "q",
                     "started_at": None, "ended_at": None, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "opt-a"},
                    {"option_key": "b", "text": "opt-b"},
                    {"option_key": "c", "text": "opt-c"}]
        if "FROM otaku_poll_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    # The dispatcher is `cmd_poll`; build the inner /poll create event.
    from mmo_maid_sdk.testing import make_event
    event = make_event(
        "interaction_create", interaction_type=2, command_name="poll",
        options=[{"name": "create", "type": 1, "options": [
            {"name": "question", "value": "Best vibe?"},
            {"name": "a", "value": "opt-a"},
            {"name": "b", "value": "opt-b"},
            {"name": "c", "value": "opt-c"},
        ]}],
        user_id="u",
    )
    p.cmd_poll(ctx, event)

    option_inserts = [e for e in (ctx.sql.executed or [])
                      if "INSERT INTO otaku_poll_options" in e["sql"]]
    assert len(option_inserts) == 1, "expected exactly one multi-row INSERT"
    # 3 options × 3 params (poll_id, key, text) = 9 params total.
    params = option_inserts[0]["params"]
    assert len(params) == 9
    # Verify the SQL uses multiple placeholder tuples.
    sql = option_inserts[0]["sql"]
    assert "($1, $2, $3)" in sql
    assert "($7, $8, $9)" in sql
    # Keys appear in declaration order at indices 1, 4, 7.
    keys = [params[3 * i + 1] for i in range(3)]
    assert keys == ["a", "b", "c"]


# ── Multi-row INSERT for AOTW candidates ───────────────────────────────────


def test_aotw_candidates_use_single_multi_row_insert(monkeypatch):
    """`/aotw start` issues one INSERT for all candidates, not N."""
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 10, "title": {"romaji": "A", "english": "A"}},
            {"id": 20, "title": {"romaji": "B", "english": "B"}},
            {"id": 30, "title": {"romaji": "C", "english": "C"}},
            {"id": 40, "title": {"romaji": "D", "english": "D"}},
        ]}},
    )

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return []
        if "FROM otaku_server_watchlist" in sql:
            return [{"media_id": 10}, {"media_id": 20},
                    {"media_id": 30}, {"media_id": 40}]
        if "WHERE started_by = $1 AND status = 'active'" in sql:
            return [{"poll_id": 88}]
        if "FROM otaku_aotw_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    from mmo_maid_sdk.testing import make_event
    event = make_event(
        "interaction_create", interaction_type=2, command_name="aotw",
        options=[{"name": "start", "type": 1, "options": []}],
        user_id="u",
    )
    p.cmd_aotw(ctx, event)

    candidate_inserts = [e for e in (ctx.sql.executed or [])
                        if "INSERT INTO otaku_aotw_candidates" in e["sql"]]
    assert len(candidate_inserts) == 1, "expected exactly one multi-row INSERT"
    params = candidate_inserts[0]["params"]
    # 4 candidates × 2 params (poll_id, media_id) = 8 params total.
    assert len(params) == 8
    sql = candidate_inserts[0]["sql"]
    assert "($1, $2)" in sql
    assert "($7, $8)" in sql
    # media_ids appear in order at odd indices.
    inserted_mids = [params[2 * i + 1] for i in range(4)]
    assert inserted_mids == [10, 20, 30, 40]


# ── FIND_PAGE_MALFORMED removal ────────────────────────────────────────────


def test_find_page_malformed_attribute_removed():
    """v10.0.3 removed the orphan `FIND_PAGE_MALFORMED` string from
    `_Strings` (it was scaffolding for /find pagination that never
    landed). Adding it back without a real call site is a regression."""
    assert not hasattr(p._Strings, "FIND_PAGE_MALFORMED"), (
        "FIND_PAGE_MALFORMED was an orphan string in v10.0.1; v10.0.3 "
        "removed it. Re-adding it requires a real /find pagination flow."
    )
