"""Regression contract for otaku v10.0.5 — SDK compliance + correctness patch.

IMMUTABLE — what shipped at v10.0.5:

NO F-STRING SQL COMPOSITION
- The SDK's `storage:sql` capability auto-rejects f-string SQL at upload
  review. v10.0.5 removed every f-string SQL composition site:
  - `_recommend_user_vector`: `f"LIMIT {RECOMMEND_VECTOR_LIMIT}"` → static
    `"LIMIT 1000"` (the SDK's hard cap).
  - `_cmd_poll_create`: `f"VALUES (...)"` builder → static `UNNEST(...)` SQL.
  - `_cmd_aotw_start`: same UNNEST replacement.
  - `_upsert_user_media`: `f"ON CONFLICT ... SET {update_sql}"` → static
    `COALESCE($N, otaku_user_media.col)` clauses. NULL params keep the
    existing row's value; non-NULL params overwrite.
  - `_ach_count_rows`: `f"WHERE ... 'anime'{extra}"` → static SQL via the
    new `_ACH_COUNT_SQL` dispatch table.

PER-PEER CAP ON _recommend_peer_vectors_batch
- v10.0.1's batched query had no per-peer cap. The SDK silently truncates
  `ctx.sql.query` results at 1000 rows. For active servers (50 peers ×
  ~50 typical ratings = 2500 rows expected), ~60% of peer data was being
  dropped arbitrarily — silently degrading recommendations.
- v10.0.5 added a `ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY rating
  DESC, media_id)` window, capped at `RECOMMEND_PEER_RATING_CAP = 20`.
  50 peers × 20 = 1000 rows max, exactly the SDK ceiling.
- Cosine similarity uses each peer's top-20 favorites for shared-title
  matching. Documented behavior: low-signal peer tails no longer
  influence recommendations.

RECOMMEND_VECTOR_LIMIT TUNED TO SDK CEILING
- v10.0.4's `RECOMMEND_VECTOR_LIMIT = 5000` was misleading because the SDK
  silently capped the underlying query at 1000. v10.0.5 set it to 1000 to
  match the actual ceiling.
"""
from __future__ import annotations

import re

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── No f-string SQL composition anywhere in __main__.py ────────────────────


def test_no_fstring_sql_composition_in_source():
    """SDK-mandated: zero f-string SQL composition in the runtime module.

    Catches regressions where a future contributor adds `f"SELECT ... {var}"`
    or `f"... VALUES {clause}"` patterns. The check is structural: it
    pattern-matches the runtime source for `f"..."` literals containing
    common SQL keywords as the first non-whitespace token.
    """
    src = (p.__file__,)
    with open(p.__file__, encoding="utf-8") as fh:
        text = fh.read()
    sql_keywords = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "WITH",
        "VALUES", "ON CONFLICT", "ORDER BY", "GROUP BY",
        "WHERE", "FROM", "LIMIT", "JOIN", "UNNEST",
    )
    # Strip out comment lines so the v10.0.5 retrospective comments don't
    # trigger the scan. We only want production code (non-comment lines).
    non_comment_lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    src_no_comments = "\n".join(non_comment_lines)
    # Look for f"<SQL>..." style composition. Allow f"<whitespace>SELECT..."
    # as a multi-line continuation; the scanner is permissive about leading
    # whitespace inside the f-string.
    for kw in sql_keywords:
        # Pattern: literal `f"` or `f'` followed by optional whitespace and the keyword.
        pattern = rf'f["\']\s*{re.escape(kw)}'
        matches = re.findall(pattern, src_no_comments)
        assert not matches, (
            f"v10.0.5 forbids f-string SQL composition. Found f-string "
            f"starting with `{kw}` in {src[0]}: {matches[:3]}"
        )


# ── Peer batch — per-peer ROW_NUMBER cap ───────────────────────────────────


def test_recommend_peer_rating_cap_constant_exists():
    """Module-level constant pinning the per-peer rating cap. 20 is the
    current shipped value (50 peers × 20 = 1000 = SDK row ceiling)."""
    assert hasattr(p, "RECOMMEND_PEER_RATING_CAP")
    assert isinstance(p.RECOMMEND_PEER_RATING_CAP, int)
    # Together with RECOMMEND_PEER_CAP, the batch row count must not exceed
    # the SDK's 1000-row ctx.sql.query limit.
    assert p.RECOMMEND_PEER_CAP * p.RECOMMEND_PEER_RATING_CAP <= 1000


def test_recommend_peer_vectors_batch_uses_row_number_window():
    """The batch query MUST cap each peer's rows so the total stays under
    the SDK's 1000-row ceiling on `ctx.sql.query`."""
    ctx = MockContext()
    captured: list = []

    def _q(sql, params=None):
        captured.append((sql, params))
        return []

    ctx.sql.query = _q
    p._recommend_peer_vectors_batch(ctx, ["a", "b", "c"])
    assert len(captured) == 1
    sql, params = captured[0]
    # The cap is enforced via ROW_NUMBER + PARTITION BY user_id.
    assert "ROW_NUMBER()" in sql
    assert "PARTITION BY user_id" in sql
    assert "ORDER BY rating DESC" in sql
    # The cap value lives in %s so it can be re-tuned without touching SQL.
    assert "rn <= %s" in sql
    # Params: [peer_ids_array, cap].
    assert params == [["a", "b", "c"], p.RECOMMEND_PEER_RATING_CAP]


# ── _recommend_user_vector tuned to 1000 ───────────────────────────────────


def test_recommend_vector_limit_matches_sdk_cap():
    """The SDK caps `ctx.sql.query` at 1000 rows. Our defensive cap should
    not exceed that — otherwise the constant lies about what really happens
    in production."""
    assert p.RECOMMEND_VECTOR_LIMIT <= 1000


def test_recommend_user_vector_limit_is_literal_in_sql():
    """The LIMIT in the SQL must be a literal number, not an f-string
    interpolation of a Python variable."""
    ctx = MockContext()
    captured: list = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._recommend_user_vector(ctx, "u")
    sql = captured[0]
    # Must contain "LIMIT 1000" verbatim. (No `LIMIT $N`, no `LIMIT {var}`.)
    assert "LIMIT 1000" in sql


# ── UNNEST replaces f-string VALUES in poll + aotw INSERTs ─────────────────


def test_poll_options_insert_uses_unnest_not_fstring(monkeypatch):
    """`/poll create` INSERT must use static UNNEST(...) syntax."""
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "WHERE started_by = %s AND question = %s" in sql:
            return [{"poll_id": 5}]
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "started_by": "u", "question": "q",
                     "started_at": None, "ended_at": None, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "x", "text": "tx"},
                    {"option_key": "y", "text": "ty"}]
        return []

    ctx.sql.query = _q
    from yourbot_sdk.testing import make_event
    event = make_event(
        "interaction_create", interaction_type=2, command_name="otaku-poll",
        options=[{"name": "create", "type": 1, "options": [
            {"name": "question", "value": "Q?"},
            {"name": "a", "value": "tx"},
            {"name": "b", "value": "ty"},
        ]}],
        user_id="u",
    )
    p.cmd_poll(ctx, event)

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_poll_options" in c["sql"]]
    assert len(inserts) == 1
    sql = inserts[0]["sql"]
    assert "UNNEST(%s::TEXT[], %s::TEXT[])" in sql
    # No f-string-style `($N, $N, $N)` repeats.
    assert "(%s, %s, %s), (%s, %s, %s)" not in sql


def test_aotw_candidates_insert_uses_unnest_not_fstring(monkeypatch):
    """`/aotw start` INSERT must use static UNNEST(...) syntax."""
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 1, "title": {"romaji": "A", "english": "A"}},
            {"id": 2, "title": {"romaji": "B", "english": "B"}},
            {"id": 3, "title": {"romaji": "C", "english": "C"}},
        ]}},
    )

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return []
        if "FROM otaku_server_watchlist" in sql:
            return [{"media_id": 1}, {"media_id": 2}, {"media_id": 3}]
        if "WHERE started_by = %s AND status = 'active'" in sql:
            return [{"poll_id": 9}]
        return []

    ctx.sql.query = _q
    from yourbot_sdk.testing import make_event
    event = make_event(
        "interaction_create", interaction_type=2, command_name="aotw",
        options=[{"name": "start", "type": 1, "options": []}],
        user_id="u",
    )
    p.cmd_aotw(ctx, event)

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_aotw_candidates" in c["sql"]]
    assert len(inserts) == 1
    sql = inserts[0]["sql"]
    assert "UNNEST(%s::INT[])" in sql
    assert "(%s, %s), (%s, %s)" not in sql


# ── COALESCE replaces f-string ON CONFLICT in _upsert_user_media ───────────


def test_upsert_user_media_uses_coalesce_static_sql():
    """`_upsert_user_media` MUST emit a single static SQL string with
    COALESCE for status + is_favorite. No f-string composition."""
    ctx = MockContext()
    p._upsert_user_media(ctx, "u", 42, status="watching")
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "expected one INSERT"
    sql = inserts[-1]["sql"]
    assert "COALESCE(%s, otaku_user_media.status)" in sql
    assert "COALESCE(%s, otaku_user_media.is_favorite)" in sql
    # Params: 7 slots — status passed → %s = "watching"; is_favorite default → %s = None.
    params = inserts[-1]["params"]
    assert len(params) == 7
    assert params[5] == "watching"
    assert params[6] is None


def test_upsert_user_media_null_preserves_existing_via_coalesce():
    """When the caller doesn't pass `status` or `is_favorite`, the
    corresponding $N slot is NULL so COALESCE keeps the existing row."""
    ctx = MockContext()
    # Caller passes only is_favorite — status slot must be NULL.
    p._upsert_user_media(ctx, "u", 42, is_favorite=True)
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    params = inserts[-1]["params"]
    assert params[5] is None  # status update is NULL → COALESCE keeps existing
    assert params[6] is True  # is_favorite update is True → COALESCE overwrites


# ── _ach_count_rows uses dispatch table, no f-string ───────────────────────


def test_ach_count_sql_dispatch_table_exists():
    """`_ACH_COUNT_SQL` MUST be a dict of static SQL strings keyed on
    `where_extra` values. Replaces the prior f-string composition."""
    assert hasattr(p, "_ACH_COUNT_SQL")
    assert isinstance(p._ACH_COUNT_SQL, dict)
    # Every key in _ACH_STATS_KEYS must have a matching SQL entry so the
    # fall-back path (outside an _ach_stats_scope) still works.
    for key in p._ACH_STATS_KEYS:
        assert key in p._ACH_COUNT_SQL, f"missing SQL for `{key!r}`"
    # And every SQL string is fully static (no f-string interpolation
    # markers; the strings must NOT contain a `{` that suggests a placeholder).
    for k, sql in p._ACH_COUNT_SQL.items():
        assert "{" not in sql, f"non-static SQL for `{k!r}`: {sql}"


def test_ach_count_rows_unknown_where_extra_returns_zero():
    """v10.0.5: unknown `where_extra` values fall through to 0 rather than
    building dynamic SQL. Locks the no-f-string contract from the inside —
    a future contributor can't sneak a new dispatch case via f-string."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"n": 999}]
    # An unknown where_extra is not in the dispatch table → 0.
    assert p._ach_count_rows(ctx, "u", "AND something_unknown = TRUE") == 0
    # An empty where_extra IS in the table → real count from the mock.
    assert p._ach_count_rows(ctx, "u") == 999
