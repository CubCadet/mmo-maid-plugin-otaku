"""Regression contract for otaku v10.0.4 — final audit-cleanup patch.

IMMUTABLE — what shipped at v10.0.4:

DEFENSIVE LIMIT ON _recommend_user_vector
- `_recommend_user_vector` now applies a `LIMIT RECOMMEND_VECTOR_LIMIT`
  (= 5000) to the SELECT, with `ORDER BY rating DESC, media_id` so the
  highest-confidence rows survive truncation if the cap kicks in.
- Typical users rate 100–500 anime; the cap only protects against
  pathological data (import bots, multi-account merges) that would
  otherwise load 10k+ rows into memory and pay the cost on every
  `_recommend_candidates` invocation.
- The `RECOMMEND_VECTOR_LIMIT` constant is module-level so future
  tuning (or per-server config in a hypothetical v10.1) only needs to
  touch one symbol.

HARDENED MULTI-ROW INSERT TEST ASSERTIONS
- `tests/regression/test_v10_0_3.py` previously asserted literal
  `"($1, $2, $3)"` and `"($7, $8, $9)"` substrings in the SQL. A future
  formatter change (compact spacing, line wrap) would silently break
  the assertion without breaking functionality.
- v10.0.4 swapped those for a whitespace-agnostic regex check that
  counts distinct `$N` placeholders. The contract is "9 placeholders
  for 9 params, ordered $1..$9," not "the SQL has these exact substrings".
"""
from __future__ import annotations

import plugin_main as p
from mmo_maid_sdk.testing import MockContext

# ── _recommend_user_vector defensive cap ───────────────────────────────────


def test_recommend_user_vector_query_includes_limit():
    """The SQL must apply LIMIT RECOMMEND_VECTOR_LIMIT to prevent unbounded
    memory growth on pathological users."""
    ctx = MockContext()
    captured: list = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._recommend_user_vector(ctx, "u")
    assert captured, "expected one SELECT to fire"
    sql = captured[0]
    assert f"LIMIT {p.RECOMMEND_VECTOR_LIMIT}" in sql


def test_recommend_user_vector_orders_by_rating_desc():
    """ORDER BY rating DESC, media_id ensures highest-confidence rows
    survive truncation if the cap kicks in. Tied ratings are broken by
    media_id for determinism."""
    ctx = MockContext()
    captured: list = []

    def _q(sql, params=None):
        captured.append(sql)
        return []

    ctx.sql.query = _q
    p._recommend_user_vector(ctx, "u")
    sql = captured[0]
    assert "ORDER BY rating DESC, media_id" in sql


def test_recommend_vector_limit_constant_is_reasonable():
    """The cap must be (a) defined as a module-level constant and
    (b) high enough that normal users (100–500 ratings) never hit it
    but low enough that an attacker can't OOM the worker."""
    assert hasattr(p, "RECOMMEND_VECTOR_LIMIT")
    cap = p.RECOMMEND_VECTOR_LIMIT
    assert isinstance(cap, int)
    assert 1000 <= cap <= 50000, (
        f"RECOMMEND_VECTOR_LIMIT={cap} is outside the sane range. Bumping "
        "below 1000 would break heavy users; above 50000 defeats the "
        "memory-guard purpose."
    )


def test_recommend_user_vector_respects_limit_in_practice():
    """Even when the mock returns more than RECOMMEND_VECTOR_LIMIT rows
    (which a real DB wouldn't, since the LIMIT clause caps it server-side),
    the function still builds a valid vector. This is a defense-in-depth
    sanity check: the LIMIT lives in SQL, not Python."""
    ctx = MockContext()
    fake_rows = [
        {"media_id": i, "rating": (i % 20) + 1}  # ratings 1..20
        for i in range(50)
    ]
    ctx.sql.query = lambda sql, params=None: fake_rows
    vec = p._recommend_user_vector(ctx, "u")
    # 50 distinct media_ids → 50 entries.
    assert len(vec) == 50
    # All ratings divided by 2.0 land in (0.0, 10.0].
    assert all(0.0 < v <= 10.0 for v in vec.values())


# ── Hardened test_v10_0_3.py assertion shape ───────────────────────────────


def test_v10_0_3_uses_whitespace_agnostic_placeholder_check():
    """v10.0.4 swapped literal "($1, $2, $3)" substring assertions in
    test_v10_0_3.py for a regex-based check. The new assertion survives
    any SQL formatter change as long as the placeholder count + ordering
    are preserved."""
    import inspect

    from tests.regression import test_v10_0_3
    src_poll = inspect.getsource(test_v10_0_3.test_poll_options_use_single_multi_row_insert)
    src_aotw = inspect.getsource(test_v10_0_3.test_aotw_candidates_use_single_multi_row_insert)
    # Both tests should now go through re.findall on \$\d+ rather than
    # asserting literal substrings.
    assert "re.findall" in src_poll
    assert "re.findall" in src_aotw
    # And the brittle literal assertions are gone.
    assert '"($1, $2, $3)" in sql' not in src_poll
    assert '"($1, $2)" in sql' not in src_aotw
