"""Regression contract for otaku v8.0.1 — migration hardening + test tightening.

IMMUTABLE — what shipped at v8.0.1:
- `_migrate_v7_to_v8` is now **step-level idempotent**: every individual DDL
  can run repeatedly without raising, and a partial failure mid-sequence
  (e.g. lock timeout during PK widening) self-heals on the next call.
- The migration acquires a `pg_advisory_xact_lock(hashtext(...))` at the
  top to serialize concurrent pool-mode workers so the RENAME can't lose
  a race under snapshot isolation.
- New idempotency probe `information_schema.key_column_usage` queries
  whether the wide PK already exists before re-doing the drop/re-add dance.
- v8.0.0's bug: a partial failure (e.g. ADD COLUMN errored after RENAME
  succeeded) would leave the table half-migrated AND the next call's
  table-name probe found `otaku_user_anime` absent, triggering the
  early-return — the half-state never healed. v8.0.1 re-probes per step
  so the migration self-heals.
- v8.0.0's other bug: two workers could both pass the probe under
  snapshot isolation, then worker B's RENAME hit "relation does not
  exist" because worker A's RENAME had already committed. v8.0.1's
  advisory lock prevents this.

The v8.0.0 test (`test_migration_helper_probes_v7_table_before_renaming`)
only verified the no-op branch. v8.0.1 adds the happy-path coverage so a
regression that silently deletes the RENAME or PK widening fires.

Also: v8.0.0 test_v3_3_0.py had a comment claiming the v8.0 contract was
"leaderboard now filters media_type='anime'" but the assertion only
checked `"status = 'completed'"`. v8.0.1 tightened the assertion to also
pin the media_type filter; verified here as a defense-in-depth check.
"""
from __future__ import annotations

import plugin_main as p
from mmo_maid_sdk.testing import MockContext

# ── Migration happy-path: v7 table exists ──────────────────────────────────


def _mock_v7_install(ctx: MockContext) -> None:
    """Configure MockContext.sql so the migration probes see the v7 table
    present and the v8 wide PK absent — the realistic upgrade path.
    """
    state = {"queries_seen": []}

    def _q(sql: str, params=None):
        state["queries_seen"].append(sql)
        # 1) Table-name probe: v7 table present, v8 absent.
        if "information_schema.tables" in sql:
            return [{"table_name": "otaku_user_anime"}]
        # 2) PK-widening probe: wide PK not yet present.
        if "information_schema.key_column_usage" in sql:
            return []
        return []

    ctx.sql.query = _q


def test_migration_executes_full_dance_when_v7_table_exists():
    """Happy-path: v7 install upgrading to v8. Every migration DDL fires."""
    ctx = MockContext()
    _mock_v7_install(ctx)
    p._migrate_v7_to_v8(ctx)

    executed = [c["sql"] for c in ctx.sql.executed]

    # Advisory lock guards the migration.
    assert any("pg_advisory_xact_lock" in s for s in executed), \
        "v8.0.1 must acquire the pool-mode advisory lock before mutating"
    # Step 1: table rename happens.
    assert any("ALTER TABLE otaku_user_anime RENAME TO otaku_user_media" in s
               for s in executed)
    # Step 2: index rename happens (IF EXISTS guard).
    assert any("ALTER INDEX IF EXISTS otaku_user_anime_user_status_added_idx" in s
               and "RENAME TO otaku_user_media_user_status_added_idx" in s
               for s in executed)
    # Step 3: ADD COLUMN media_type.
    assert any("ADD COLUMN IF NOT EXISTS media_type" in s for s in executed)
    # Step 4: PK widening — both DROPs plus the wider ADD CONSTRAINT.
    assert any("DROP CONSTRAINT IF EXISTS otaku_user_anime_pkey" in s
               for s in executed)
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s
               and "PRIMARY KEY (user_id, media_id, media_type)" in s
               for s in executed)


def test_migration_skips_pk_widen_when_wide_pk_already_present():
    """Idempotency: if a previous run already widened the PK, the second run
    must NOT re-execute the drop/re-add dance."""
    ctx = MockContext()

    def _q(sql: str, params=None):
        if "information_schema.tables" in sql:
            # Both tables present means the rename already happened (the
            # v7 name lingers if a manual repair created a stub). v8.0.1
            # treats this as "no rename needed" — only widen if needed.
            return [{"table_name": "otaku_user_media"}]
        if "information_schema.key_column_usage" in sql:
            # Wide PK already exists.
            return [{"one": 1}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    executed = [c["sql"] for c in ctx.sql.executed]
    # No DROP CONSTRAINT or ADD CONSTRAINT should fire when the wide PK is
    # already in place.
    assert not any("DROP CONSTRAINT" in s for s in executed), \
        "must not re-drop a constraint when the wide PK already exists"
    assert not any("ADD CONSTRAINT otaku_user_media_pkey" in s
                   for s in executed), \
        "must not re-add the PK when it's already there"


def test_migration_self_heals_after_partial_failure():
    """v8.0.0 bug: if step 3 errored after step 1 succeeded, the next call's
    probe found `otaku_user_anime` absent and early-returned, leaving the
    table half-migrated forever. v8.0.1 self-heals: with the v8 table
    already renamed but the column / PK still missing, the migration
    correctly skips RENAME and continues with ADD COLUMN + PK widening.
    """
    ctx = MockContext()

    def _q(sql: str, params=None):
        if "information_schema.tables" in sql:
            # v7 already renamed, but the column add never finished.
            return [{"table_name": "otaku_user_media"}]
        if "information_schema.key_column_usage" in sql:
            # Wide PK not yet there.
            return []
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    executed = [c["sql"] for c in ctx.sql.executed]
    # No re-rename of the already-renamed table.
    assert not any("ALTER TABLE otaku_user_anime RENAME" in s for s in executed)
    # But the column add and PK widening MUST fire to complete the migration.
    assert any("ADD COLUMN IF NOT EXISTS media_type" in s for s in executed)
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in executed)


def test_migration_noop_on_fresh_install():
    """A fresh v8 install has neither v7 nor v8 table at probe time. The
    migration must skip everything (the _SCHEMA_DDL CREATE TABLE later
    in _bootstrap_schema will create the v8 table fresh)."""
    ctx = MockContext()
    p._migrate_v7_to_v8(ctx)  # MockContext default: query returns []

    executed = [c["sql"] for c in ctx.sql.executed]
    # Advisory lock still acquired (defensive; cheap).
    assert any("pg_advisory_xact_lock" in s for s in executed)
    # But no DDL on the tables themselves.
    assert not any("ALTER TABLE otaku_user_anime" in s for s in executed)
    assert not any("ALTER TABLE otaku_user_media" in s for s in executed)


# ── Advisory lock contract ─────────────────────────────────────────────────


def test_migration_acquires_advisory_lock_first():
    """The advisory lock must be the FIRST execute call so that any
    subsequent step's lock-contention auto-releases on a partial failure
    (the lock is transactional)."""
    ctx = MockContext()
    _mock_v7_install(ctx)
    p._migrate_v7_to_v8(ctx)

    executed_sqls = [c["sql"] for c in ctx.sql.executed]
    # The lock is acquired via `SELECT pg_advisory_xact_lock(...)` — itself
    # a SELECT — so check the lock index appears BEFORE any ALTER index.
    lock_idx = next(
        (i for i, s in enumerate(executed_sqls) if "pg_advisory_xact_lock" in s),
        -1,
    )
    alter_idx = next(
        (i for i, s in enumerate(executed_sqls) if s.startswith("ALTER")),
        -1,
    )
    assert lock_idx != -1, "advisory lock never acquired"
    assert alter_idx != -1, "happy-path test should fire at least one ALTER"
    assert lock_idx < alter_idx, "advisory lock must come before any ALTER"


# ── Leaderboard filter assertion (tightening test_v3_3_0.py:63) ───────────


# ── Column-anchor companions for v8.0's `X in params` shifts ──────────────
#
# v8.0.0's regression-fix edits in test_v2_*.py converted positional
# `params[N] == X` assertions to `X in params` membership checks (because
# v8 added media_type to the INSERT column list, shifting indices). The
# audit flagged that this lost the column-anchoring intent:
#   - `True in params` could match a future integer 1 (Python: True == 1)
#   - `"watching" in params` could match any string column equal to "watching"
#   - `18 in params` could match a media_id that happens to be 18
#   - `"completed" in params` could match any string column equal to "completed"
#
# These tests pair each contract with a SQL-substring anchor that pins
# WHICH column the value lives in — so a future regression that, say,
# stops setting `is_favorite` but still emits a row with `True` somewhere
# else in params will be caught.
#
# The v2 tests stay immutable; these are net-new tighter contracts.


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    from mmo_maid_sdk.testing import make_event
    return make_event(
        "interaction_create", interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


_FAV_SAMPLE = {
    "id": 555, "title": {"romaji": "X", "english": "X"},
    "description": "—", "coverImage": {"large": "u"}, "bannerImage": None,
    "averageScore": 80, "popularity": 100, "format": "TV", "episodes": 12,
    "status": "FINISHED", "season": "SUMMER", "seasonYear": 2024,
    "genres": ["Action"], "siteUrl": "https://anilist.co/anime/555",
}


# regression-fix (v10.0.5): the v8.0.1 doctrine asserted the DO UPDATE clause
# contained `is_favorite = EXCLUDED.is_favorite` / `status = EXCLUDED.status`
# as anchor strings, defending against regressions that drop the column from
# the update path. v10.0.5 rewrote `_upsert_user_media` to use COALESCE
# (`is_favorite = COALESCE($7, ...)`, `status = COALESCE($6, ...)`) because
# the prior f-string SET-clause builder was banned by the SDK's "no f-string
# SQL" rule. The same anti-regression intent is preserved: the assertions
# now look for the COALESCE shape on the corresponding column.
def test_favorite_upsert_sql_anchors_is_favorite_column():
    """The /favorite-add INSERT's DO UPDATE must touch is_favorite via COALESCE."""
    import json
    ctx = MockContext()
    ctx.kv.set("last_anime:user:anchor1", 555, ttl_seconds=3600)
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": _FAV_SAMPLE}}),
    )
    p.cmd_favorite(ctx, _slash("favorite", {}, user_id="anchor1"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "favorite must INSERT"
    sql = inserts[-1]["sql"]
    assert "is_favorite = COALESCE(" in sql and "otaku_user_media.is_favorite" in sql, \
        "favorite's DO UPDATE clause must touch is_favorite via COALESCE"


# regression-fix (v10.0.5): see comment on test_favorite_upsert_sql_anchors_is_favorite_column.
def test_watch_upsert_sql_anchors_status_column():
    """/watch's INSERT's DO UPDATE must touch status via COALESCE."""
    import json
    ctx = MockContext()
    ctx.kv.set("last_anime:user:anchor2", 555, ttl_seconds=3600)
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": _FAV_SAMPLE}}),
    )
    p.cmd_watch(ctx, _slash("watch", {"status": "watching"}, user_id="anchor2"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "watch must INSERT"
    sql = inserts[-1]["sql"]
    assert "status = COALESCE(" in sql and "otaku_user_media.status" in sql, \
        "watch's DO UPDATE clause must touch status via COALESCE"


def test_rate_upsert_sql_anchors_rating_column():
    """/rate's INSERT must have `rating = EXCLUDED.rating` in DO UPDATE so a
    regression that stops setting rating can't pass on `18 in params`
    coincidentally matching a media_id."""
    import json
    ctx = MockContext()
    ctx.kv.set("last_anime:user:anchor3", 555, ttl_seconds=3600)
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": _FAV_SAMPLE}}),
    )
    p.cmd_rate(ctx, _slash("rate", {"score": 9.0}, user_id="anchor3"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "rate must INSERT"
    assert "rating = EXCLUDED.rating" in inserts[-1]["sql"], \
        "rate's DO UPDATE clause must touch rating"


def test_progress_upsert_sql_anchors_episodes_watched_column():
    """/progress's INSERT must have `episodes_watched = EXCLUDED.episodes_watched`
    in DO UPDATE so a regression that stops updating progress can't pass on
    `12 in params` coincidentally matching another column."""
    import json
    ctx = MockContext()
    ctx.kv.set("last_anime:user:anchor4", 901, ttl_seconds=3600)
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": {**_FAV_SAMPLE, "id": 901, "episodes": 12}}}),
    )
    p.cmd_progress(ctx, _slash("progress", {"episodes": 12}, user_id="anchor4"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "progress must INSERT"
    assert "episodes_watched = EXCLUDED.episodes_watched" in inserts[-1]["sql"], \
        "progress's DO UPDATE clause must touch episodes_watched"


def test_leaderboard_completed_query_filters_media_type_anime():
    """v7.x leaderboard contract: filter status='completed'. v8.0 added an
    implicit media_type='anime' filter so manga rows don't leak in. v8.0.0's
    test_v3_3_0.py:63 noted this in a comment but didn't assert it; v8.0.1
    pins it explicitly here so a regression dropping the filter fires.
    """
    from mmo_maid_sdk.testing import make_event

    ctx = MockContext()
    captured: dict = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return [{"user_id": "a", "n": 1}]

    ctx.sql.query = _q
    p.cmd_leaderboard(ctx, make_event(
        "interaction_create", interaction_type=2,
        command_name="leaderboard", options=[], user_id="u",
    ))
    assert "media_type = 'anime'" in captured["sql"], \
        "leaderboard MUST filter to anime rows in v8.0+"
    assert "status = 'completed'" in captured["sql"], \
        "leaderboard MUST still filter completed-status (v3.3 contract)"
