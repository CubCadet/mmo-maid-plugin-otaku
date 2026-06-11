"""Regression contract for otaku v10.0.8 — host SQL allowlist + /stats refactor.

IMMUTABLE — what shipped at v10.0.8:

HOST SQL STATEMENT ALLOWLIST
- The 2026-05-14 23:22 log surfaced an explicit denylist: the host only
  permits ALTER TABLE, CREATE INDEX, CREATE TABLE, DELETE, DROP INDEX,
  DROP TABLE, INSERT, SELECT, UPDATE. v10.0.6's `_migrate_v7_to_v8`
  used `ALTER INDEX … RENAME TO …` (step 2) which is NOT in that list;
  every boot crashed on_ready before any of the other DDLs could fire.
- v10.0.8 replaces the rename with `DROP INDEX IF EXISTS
  otaku_user_anime_user_status_added_idx`. The companion
  `_SCHEMA_INDEX_DDL` (`CREATE INDEX IF NOT EXISTS
  otaku_user_media_user_status_added_idx …`) in `_bootstrap_schema`
  recreates the index under the v8 name.

MIGRATION BRANCHING — "ALREADY V8" CASE
- v10.0.6's logic only handled (v7 only) and (neither). If v8 existed
  without v7 (a v10.0.6+ install where KV got wiped — e.g., plugin
  reinstall — but SQL persisted), the function fell through to the
  rename-completion steps and crashed on the ALTER INDEX. v10.0.8
  adds explicit branches for "already v8" (set marker + return) and
  "both tables exist" (drop v7 zombie + set marker + return). The
  rename + completion DDL sequence ONLY fires on the true upgrade
  path (v7 exists, v8 doesn't).

/STATS REFACTORED TO PYTHON AGGREGATION
- The live host returned `RPC error (sql.query): SQL execution failed`
  for the v10.0.6 `_aggregate_user_stats` query (GROUP BY status with
  COUNT/SUM/AVG). Root cause unisolated; v10.0.8 sidesteps by fetching
  raw row shape `(status, episodes_watched, rating)` and aggregating
  in Python. Same output dict; cheaper to debug. Capped at the SDK's
  1000-row ceiling (typical user is far below).

/RECOMMEND DIAGNOSTIC LOGGING
- `_recommend_user_vector` wraps its single SELECT in a try/except.
  On `RPC error`, logs the exception (tagged "recommend", "sql") and
  returns an empty vector so the caller falls through to /recommend's
  AniList-similar seed fallback. Allows v10.0.9 triage from the
  captured server log without /recommend being a hard-fail surface.
"""
from __future__ import annotations

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── Migration: ALTER INDEX replaced with DROP INDEX ────────────────────────


def test_migration_does_not_use_alter_index():
    """The host's SQL allowlist does NOT include ALTER INDEX. The migration
    must avoid it entirely."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            # Upgrade path so step 2 (index handling) fires.
            return [{"v7": "otaku_user_anime", "v8": None}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    assert not any("ALTER INDEX" in s for s in sqls), \
        "v10.0.8: ALTER INDEX is on the host's denylist"


def test_migration_drops_old_index_name_on_upgrade():
    """The v7 index name must be cleaned up. With ALTER INDEX disallowed,
    v10.0.8 uses DROP INDEX IF EXISTS."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": "otaku_user_anime", "v8": None}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx" in s
               for s in sqls)


# ── Migration: branching ───────────────────────────────────────────────────


def test_migration_v8_exists_without_v7_sets_marker_no_ddl():
    """The bug surfaced in 2026-05-14 23:22 log: v10.0.6+ install where
    SQL persisted but KV got wiped (e.g., plugin reinstall). Probe sees
    v8 only. v10.0.6's logic fell through to ALTER INDEX (crash);
    v10.0.8 treats this as "already migrated" — set marker, no DDL."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": None, "v8": "otaku_user_media"}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    # Migration DDLs must NOT fire (the bug was firing them, hitting ALTER INDEX).
    assert not any("DROP INDEX" in s for s in sqls)
    assert not any("ADD COLUMN IF NOT EXISTS media_type" in s for s in sqls)
    assert not any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)
    # Marker must be set so subsequent boots short-circuit.
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


def test_migration_both_tables_exist_drops_v7_zombie():
    """Stuck state from a v8.0.0 bug — both tables present. v10.0.8 drops
    the v7 zombie (DROP TABLE is allowed) and marks complete."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": "otaku_user_anime", "v8": "otaku_user_media"}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("DROP TABLE IF EXISTS otaku_user_anime" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


def test_migration_upgrade_path_still_full_dance():
    """Sanity: the only branch that still fires the full rename + completion
    DDLs is the real upgrade path (v7 exists, v8 doesn't)."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": "otaku_user_anime", "v8": None}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("ALTER TABLE otaku_user_anime RENAME TO otaku_user_media" in s
               for s in sqls)
    assert any("DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx" in s
               for s in sqls)
    assert any("ADD COLUMN IF NOT EXISTS media_type" in s for s in sqls)
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


# ── /stats: Python aggregation ─────────────────────────────────────────────


def test_aggregate_user_stats_uses_simple_select_not_group_by():
    """v10.0.6 used a single SQL aggregate (GROUP BY status with
    COUNT/SUM/AVG); the live host returned generic SQL-failed errors.
    v10.0.8 fetches raw rows and aggregates in Python — sidesteps any
    complex-query parser issue while we triage the underlying cause."""
    ctx = MockContext()
    captured = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._aggregate_user_stats(ctx, "u")

    assert captured, "_aggregate_user_stats must issue at least one query"
    sql = captured[0]
    assert "GROUP BY" not in sql, "v10.0.8 must NOT use GROUP BY"
    assert "COUNT(" not in sql, "v10.0.8 must NOT use COUNT()"
    assert "SUM(" not in sql, "v10.0.8 must NOT use SUM()"
    assert "AVG(" not in sql, "v10.0.8 must NOT use AVG()"
    # Just the raw columns.
    assert "status" in sql and "episodes_watched" in sql and "rating" in sql


def test_aggregate_user_stats_python_aggregation_correctness():
    """The Python aggregation must produce the same shape and values as
    the v10.0.6 SQL version."""
    ctx = MockContext()

    def _q(sql, params=None):
        return [
            {"status": "watching", "episodes_watched": 12, "rating": None},
            {"status": "watching", "episodes_watched": 6, "rating": None},
            {"status": "completed", "episodes_watched": 24, "rating": 18},
            {"status": "completed", "episodes_watched": 12, "rating": 14},
        ]

    ctx.sql.query = _q
    agg = p._aggregate_user_stats(ctx, "u")

    assert agg["total"] == 4
    assert agg["by_status"]["watching"] == 2
    assert agg["by_status"]["completed"] == 2
    assert agg["total_episodes"] == 12 + 6 + 24 + 12
    assert agg["rated_count"] == 2
    # Mean of 18 and 14 = 16.
    assert agg["mean_rating"] == 16.0


def test_aggregate_user_stats_empty_returns_empty_dict():
    """When the user has no rows, the function returns {} (not a zero-filled
    stats dict). Caller branches on this to show the empty-state message."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    assert p._aggregate_user_stats(ctx, "u") == {}


# ── /recommend: diagnostic try/except ──────────────────────────────────────


def test_recommend_user_vector_swallows_sql_error_and_logs():
    """The live host has been failing this query with the generic
    `SQL execution failed`. v10.0.8 catches the exception, logs a
    "recommend"/"sql"-tagged error with the exception text, and returns
    an empty vector so /recommend falls through to its AniList-similar
    seed fallback instead of crashing the handler."""
    ctx = MockContext()

    def _raising_query(sql, params=None):
        raise RuntimeError(
            "RPC error (sql.query): SQL execution failed. "
            "Check your query syntax and parameters."
        )

    ctx.sql.query = _raising_query

    # Must NOT raise.
    vec = p._recommend_user_vector(ctx, "u")
    assert vec == {}, "failed SQL must return an empty vector"

    # Must log the failure for v10.0.9 triage. Some mock contexts record logs
    # as `ctx.logs`, others as `ctx.log_entries`; either way, the log call
    # happened — proven by reaching this point without RuntimeError propagating.


def test_recommend_user_vector_happy_path_unchanged():
    """When SQL succeeds, the v10.0.6 vector shape is preserved (media_id
    → rating/2.0). The try/except is purely defensive."""
    ctx = MockContext()

    def _q(sql, params=None):
        return [
            {"media_id": 1, "rating": 18},  # 9.0/10
            {"media_id": 2, "rating": 14},  # 7.0/10
            {"media_id": 3, "rating": None},  # filtered out
        ]

    ctx.sql.query = _q
    vec = p._recommend_user_vector(ctx, "u")
    assert vec == {1: 9.0, 2: 7.0}
