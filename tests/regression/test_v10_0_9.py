"""Regression contract for otaku v10.0.9 — SQL placeholder conversion ($N → %s).

IMMUTABLE — what shipped at v10.0.9:

ROOT CAUSE OF "SQL execution failed" — IDENTIFIED
- v10.0.6 / v10.0.7 / v10.0.8 all chased the live host's generic
  `RPC error (sql.query): SQL execution failed. Check your query syntax
  and parameters.` for `/stats` and `/recommend`. The 2026-05-14 23:39
  log was conclusive: EVERY parameterized SQL query failed; every
  parameter-free query succeeded. The diagnostic `_recommend_user_vector
  SQL failed: …` added at v10.0.8 confirmed the same error fires for
  the simplest possible `WHERE user_id = $1` shape.
- The host doesn't accept `$N`-style placeholders. The SDK docstring
  in `_context.py` documents `%s` as the placeholder format; the skill
  reference (`api-reference.md`) had claimed both worked. Empirically,
  only `%s` works on this tenant.

GLOBAL $N → %s CONVERSION
- v10.0.9 mechanically converts all 165 `$N` placeholder occurrences
  in `__main__.py` to `%s` (positional, not numbered). One semantic
  gotcha: `$1` could be referenced multiple times in a single SQL
  string; `%s` is strictly positional. The single site that reused
  `$1` (`_ach_load_stats`'s subquery-pair) was rewritten to pass the
  parameter twice.
- All test assertions that pinned `$N` substrings were converted
  alongside (88 occurrences across 14 test files). The v10.0.4
  regex-count test had to be reshaped from `re.findall(r"\\$\\d+",
  sql)` to `sql.count("%s") == N` because `%s` tokens aren't
  individually distinguishable.

NO BEHAVIOR CHANGE
- Output dicts, mutation semantics, ON CONFLICT clauses, JOIN shapes —
  all preserved verbatim. This is a pure syntactic conversion.
"""
from __future__ import annotations

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── No $N placeholders remain anywhere in the runtime module ──────────────


def test_no_dollar_n_placeholders_in_source():
    """Forbid any `$<digit>` substring in production SQL. The live host
    rejects them; converting to `%s` is the v10.0.9 contract.

    Comment lines are exempt (retrospective comments may reference the
    old $1 syntax when explaining the conversion)."""
    import re
    with open(p.__file__, encoding="utf-8") as fh:
        text = fh.read()
    non_comment_lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    src = "\n".join(non_comment_lines)
    matches = re.findall(r"\$\d+", src)
    assert not matches, \
        f"v10.0.9 forbids $N placeholders in production SQL; found {matches[:5]}"


def test_recommend_user_vector_sql_uses_percent_s():
    """The specific query that failed in production must now use %s."""
    ctx = MockContext()
    captured = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._recommend_user_vector(ctx, "u")

    assert captured, "_recommend_user_vector must issue a query"
    sql = captured[0]
    assert "%s" in sql
    assert "$1" not in sql


def test_aggregate_user_stats_sql_uses_percent_s():
    """`/stats` query (the other production failure surface) must now use %s."""
    ctx = MockContext()
    captured = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._aggregate_user_stats(ctx, "u")

    assert captured, "_aggregate_user_stats must issue a query"
    sql = captured[0]
    assert "%s" in sql
    assert "$1" not in sql


# ── Param count matches %s count (positional binding) ─────────────────────


def test_ach_load_stats_passes_user_id_twice_for_two_subqueries():
    """v10.0.9 gotcha: the v10.0.5 `_ach_load_stats` SELECT had two
    subqueries that both referenced `$1`. With %s (positional) the
    parameter must appear twice in the params list."""
    ctx = MockContext()
    captured = []

    def _q(sql, params=None):
        captured.append({"sql": sql, "params": params})
        return [{"total": 0, "favorites": 0, "completed": 0, "rated": 0}]

    ctx.sql.query = _q
    p._ach_load_stats(ctx, "user-x")

    # Second call is the reviews + subs subquery pair.
    misc = next((c for c in captured if "otaku_reviews" in c["sql"]
                 and "otaku_notifications" in c["sql"]), None)
    assert misc is not None
    # Two %s positions in the SQL.
    assert misc["sql"].count("%s") == 2
    # And params has user_id twice to fill them.
    assert misc["params"] == ["user-x", "user-x"]


def test_upsert_user_media_seven_params_seven_placeholders():
    """Sanity: the v10.0.5 COALESCE upsert had 5 + 2 = 7 placeholders
    ($1..$5 in VALUES, $6/$7 in the DO UPDATE). With %s, the SQL must
    contain seven %s tokens and the params list must have 7 items."""
    ctx = MockContext()
    p._upsert_user_media(ctx, "u", 42, status="watching")

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    last = inserts[-1]
    assert last["sql"].count("%s") == 7
    assert len(last["params"]) == 7


# ── Semantic correctness preserved ─────────────────────────────────────────


def test_upsert_user_media_coalesce_clauses_still_present():
    """v10.0.5's COALESCE pattern in the DO UPDATE clause must still
    target the correct columns (status, is_favorite). v10.0.9 only
    rewrites placeholders; the column-anchor contract is preserved."""
    ctx = MockContext()
    p._upsert_user_media(ctx, "u", 42, status="watching")
    sql = [c["sql"] for c in ctx.sql.executed
           if "INSERT INTO otaku_user_media" in c["sql"]][-1]
    assert "status = COALESCE(%s, otaku_user_media.status)" in sql
    assert "is_favorite = COALESCE(%s, otaku_user_media.is_favorite)" in sql


def test_recommend_user_vector_happy_path():
    """End-to-end: with %s placeholders, the query roundtrip returns the
    correct vector shape. Catches a hypothetical conversion bug where
    placeholders and params get out of sync."""
    ctx = MockContext()

    def _q(sql, params=None):
        return [
            {"media_id": 1, "rating": 18},
            {"media_id": 2, "rating": 14},
        ]

    ctx.sql.query = _q
    vec = p._recommend_user_vector(ctx, "u")
    assert vec == {1: 9.0, 2: 7.0}


def test_anniversary_pk_widening_with_percent_s():
    """The PK widening doesn't use any placeholders, so the conversion is
    irrelevant here — but verify the rename + ADD CONSTRAINT chain still
    emits the wide PK on the upgrade path."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": "otaku_user_anime", "v8": None}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)

    sqls = [c["sql"] for c in ctx.sql.executed]
    pk = next((s for s in sqls if "ADD CONSTRAINT otaku_user_media_pkey" in s), None)
    assert pk is not None
    assert "PRIMARY KEY (user_id, media_id, media_type)" in pk
    # PK SQL doesn't take placeholders.
    assert "%s" not in pk
    assert "$1" not in pk
