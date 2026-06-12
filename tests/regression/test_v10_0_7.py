"""Regression contract for otaku v10.0.7 — bootstrap resilience + Jikan/Kitsu transport.

IMMUTABLE — what shipped at v10.0.7:

SCHEMA-VERSION KV MARKER + DDL RATE-LIMIT RESILIENCE
- The host enforces a strict DDL rate limit (5 statements/hour per tenant).
  v10.0.6's `_bootstrap_schema` issued 16 DDLs on fresh install; it hit
  the cap at the 6th statement and crashed `on_ready` before half the
  tables existed. Re-runs on subsequent worker boots crashed the same
  way (every CREATE/ALTER IF NOT EXISTS counts against the budget even
  when it's a no-op).
- v10.0.7 fixes:
  - New KV marker `otaku:schema_version` (= "10.0.7") records full
    completion. Subsequent boots: one kv.get + early return, zero DDL.
  - On `RateLimitError` mid-bootstrap the function logs a warning and
    returns cleanly — the marker stays unset, so the next worker
    restart resumes. Each DDL is `IF NOT EXISTS` so re-running prior
    ones is safe.
  - `rating` and `episodes_watched` columns folded into the
    `otaku_user_media` CREATE TABLE so a fresh install carries them
    natively without standalone ALTER ADD COLUMN statements. The
    legacy ALTERs remain in the bootstrap list to cover existing
    v2.0-era tables that pre-date the columns.

JIKAN / KITSU TRANSPORT FIX
- `_jikan_query` and `_kitsu_query` shipped at v9.1.0 calling
  `ctx.http.get(url, params={}, headers={...})`. The SDK's
  `ctx.http.get(url, *, headers=None)` doesn't accept a `params`
  kwarg — every call raised `TypeError: unexpected keyword argument
  'params'` and was caught by the bare-Exception clause, showing up
  as "jikan call failed" / "kitsu call failed" in the logs. The
  AniList → Jikan → Kitsu fallback chain has been silently broken
  since v9.1.0 — only visible now because AniList itself started
  returning unparseable JSON, exposing the fallbacks.
- v10.0.7: params are `urllib.parse.urlencode`'d into the URL query
  string. Response shape also corrected: SDK returns `status` and
  `body_bytes`; the prior code read `status_code` and `body` (a
  pre-existing typo). Both old field names are kept as defensive
  fallbacks.
"""
from __future__ import annotations

import json

import plugin_main as p
from yourbot_sdk import RateLimitError
from yourbot_sdk.testing import MockContext

# ── Schema-version marker short-circuits bootstrap ─────────────────────────


def test_bootstrap_short_circuits_when_schema_version_marker_set():
    """Fast path: if the KV marker matches the current version, the function
    must return BEFORE issuing any SQL. This is what protects production
    installs from re-paying the host's 5-DDL/hour budget on every boot."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)
    # regression-fix (v10.0.12): steady state now includes the one-time
    # wide-PK verification marker; without it the fast path runs a single
    # SELECT probe (not DDL — the budget this contract protects is intact).
    # The probe behavior itself is pinned in test_v10_0_12.py.
    ctx.kv.set(p._SCHEMA_PK_VERIFIED_KV, "1")
    p._bootstrap_schema(ctx)
    assert ctx.sql.executed == [], \
        "marker-present fast path must issue zero SQL calls"


def test_bootstrap_sets_schema_version_marker_on_success():
    """After a full successful bootstrap, the marker MUST be set so the
    next call short-circuits."""
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


def test_current_schema_version_constant_pinned():
    """Bumping the constant invalidates every existing tenant's marker and
    forces a full re-bootstrap. This test pins the value so the bump is
    intentional, not a typo."""
    assert p._CURRENT_SCHEMA_VERSION == "10.0.7"
    assert p._SCHEMA_VERSION_KV == "otaku:schema_version"


def test_bootstrap_skips_old_marker_value():
    """If the marker has a stale version string, the bootstrap MUST still
    run (otherwise schema upgrades would silently skip)."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, "10.0.6")  # stale
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)
    # And the marker advances to the new version.
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


# ── DDL rate-limit handling ────────────────────────────────────────────────


def test_bootstrap_catches_rate_limit_and_does_not_crash():
    """The 5 DDL/hour host cap means a fresh install will hit RateLimitError
    mid-bootstrap. The function MUST NOT propagate the exception — that
    would crash on_ready and leave the schema half-applied with no path
    to recovery. Instead it logs a warning and returns cleanly."""
    ctx = MockContext()

    call_count = {"n": 0}
    def _ratelimited_execute(sql, params=None):
        call_count["n"] += 1
        ctx.sql.executed.append({"sql": sql, "params": params})
        if call_count["n"] >= 4:  # mid-bootstrap rate limit
            raise RateLimitError("DDL rate limit: max 5 DDL statements per hour", retry_after=60)
        return 0

    ctx.sql.execute = _ratelimited_execute
    # MUST NOT raise.
    p._bootstrap_schema(ctx)

    # And the marker must NOT be set, so the next call resumes.
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) != p._CURRENT_SCHEMA_VERSION, \
        "marker must stay unset on partial bootstrap so the next boot resumes"


def test_bootstrap_partial_then_complete_two_call_sequence():
    """Real recovery flow: first boot hits the rate limit partway through;
    second boot (e.g., after a worker restart and budget refresh) completes
    the remaining DDLs and sets the marker."""
    ctx = MockContext()
    state = {"limit_hits": 1}  # rate-limit once, then let through

    real_execute = ctx.sql.execute
    def _execute(sql, params=None):
        if "ALTER TABLE" in sql or "CREATE TABLE" in sql or "CREATE INDEX" in sql:
            if state["limit_hits"] > 0 and "otaku_watch_parties" in sql:
                state["limit_hits"] -= 1
                # Record the attempt before raising so the test can audit.
                ctx.sql.executed.append({"sql": sql, "params": params})
                raise RateLimitError("DDL rate limit: max 5 DDL statements per hour", retry_after=60)
        return real_execute(sql, params)

    ctx.sql.execute = _execute

    # First call: partial — marker NOT set.
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) != p._CURRENT_SCHEMA_VERSION

    # Second call: budget refreshed, completes.
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


# ── Column consolidation: fresh installs ───────────────────────────────────


def test_schema_ddl_includes_rating_and_episodes_columns():
    """v10.0.7 folds v2.1's `rating` and v2.2's `episodes_watched` columns
    directly into the otaku_user_media CREATE TABLE. Fresh installs get
    them without needing the standalone ALTER ADD COLUMN statements
    (which would otherwise burn 2 of the 5 DDL/hour budget on first
    boot). The legacy ALTERs remain in `_bootstrap_schema` to cover
    existing v2.0-era tables that pre-date the columns."""
    assert "rating SMALLINT" in p._SCHEMA_DDL
    assert "episodes_watched SMALLINT" in p._SCHEMA_DDL


# ── Jikan transport ─────────────────────────────────────────────────────────


def test_jikan_query_does_not_pass_params_kwarg_to_http_get():
    """The SDK's `ctx.http.get(url, *, headers=None)` does NOT accept a
    `params` kwarg. The v9.1.0 helper passed `params={...}` and silently
    failed every call. v10.0.7 URL-encodes the params instead."""
    ctx = MockContext()
    ctx.http.mock_response(
        "api.jikan.moe", status=200,
        body=json.dumps({"data": [{"mal_id": 1, "title": "X"}]}),
    )
    result = p._jikan_query(ctx, "/anime", {"q": "death note", "limit": 1})

    # If the bug were present, this would raise TypeError before reaching
    # the mock — `result` would be None and the request list would be empty.
    assert result is not None, "jikan call must reach the proxy"
    assert ctx.http.requests, "jikan call must record at least one request"
    req = ctx.http.requests[0]
    # Params encoded into the URL query string, not passed separately.
    assert "q=death+note" in req["url"] or "q=death%20note" in req["url"]
    assert "limit=1" in req["url"]


def test_jikan_query_reads_body_bytes_field():
    """SDK returns `body_bytes`, not `body`. v10.0.7 reads the new field
    (with `body` as a defensive fallback)."""
    ctx = MockContext()
    ctx.http.mock_response(
        "api.jikan.moe", status=200,
        body=json.dumps({"data": [{"mal_id": 7, "title": "Y"}]}),
    )
    result = p._jikan_query(ctx, "/anime", {"q": "y"})
    assert result == [{"mal_id": 7, "title": "Y"}]


def test_jikan_query_handles_url_with_existing_querystring():
    """If the path already contains a `?`, additional params append with
    `&`; otherwise `?`."""
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=200, body='{"data": []}')
    p._jikan_query(ctx, "/anime?type=tv", {"q": "x"})
    url = ctx.http.requests[0]["url"]
    assert url.endswith("&q=x") or "&q=x&" in url, f"unexpected URL: {url}"


# ── Kitsu transport ─────────────────────────────────────────────────────────


def test_kitsu_query_does_not_pass_params_kwarg_to_http_get():
    ctx = MockContext()
    ctx.http.mock_response(
        "kitsu.io", status=200,
        body=json.dumps({"data": [{"id": "1", "type": "anime", "attributes": {}}]}),
    )
    result = p._kitsu_query(ctx, "/anime", {"filter[text]": "Frieren"})

    assert result is not None, "kitsu call must reach the proxy"
    assert ctx.http.requests, "kitsu call must record at least one request"
    req = ctx.http.requests[0]
    # `filter[text]` should be URL-encoded.
    assert "filter" in req["url"] and "Frieren" in req["url"]


def test_kitsu_query_reads_body_bytes_field():
    ctx = MockContext()
    ctx.http.mock_response(
        "kitsu.io", status=200,
        body=json.dumps({"data": [{"id": "9", "type": "anime", "attributes": {}}]}),
    )
    result = p._kitsu_query(ctx, "/anime", {"q": "x"})
    assert result == [{"id": "9", "type": "anime", "attributes": {}}]


# ── End-to-end: AniList failure surfaces Jikan results ─────────────────────


def test_search_media_falls_back_to_jikan_when_anilist_fails(monkeypatch):
    """The point of v9.1's multi-source aggregation: when AniList fails,
    Jikan should serve results. v10.0.6's transport bug silently broke
    this fallback. v10.0.7 must restore it."""
    ctx = MockContext()

    # AniList returns nothing (simulate unparseable JSON / outage).
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)

    # Jikan returns a real result.
    ctx.http.mock_response(
        "api.jikan.moe", status=200,
        body=json.dumps({"data": [{
            "mal_id": 42,
            "title": "Frieren",
            "synopsis": "—",
            "score": 8.5,
            "episodes": 28,
            "status": "Finished Airing",
            "genres": [{"name": "Adventure"}],
            "images": {"jpg": {"large_image_url": "u"}},
            "url": "https://myanimelist.net/anime/42",
        }]}),
    )

    media = p._search_media(ctx, "frieren", media_type="anime")
    assert media is not None, "Jikan fallback must produce a media dict"
    # v9.1's canonicaliser labels Jikan results with source="mal" (Jikan
    # is a MAL proxy). The test just needs to confirm the AniList →
    # Jikan fallback actually fires.
    assert media.get("source") in {"jikan", "mal"}
