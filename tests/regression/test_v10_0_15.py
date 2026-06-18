"""v10.0.15 regression contracts — per-(source, outcome) HTTP metrics.

The 2026-06-11/12 proxy-compression incident was only diagnosed after an
anomaly *log line* was added and a live retest was hand-scraped. v10.0.15 adds
a time-series metric so the next upstream/proxy degradation is visible on the
dev-portal dashboard without a log scrape: every real HTTP transport result
emits exactly one `http.request` datapoint tagged `{source, outcome}`.

Contract:
  - outcome ∈ {ok, timeout, rate, validation, error, non_2xx, bad_body,
    api_errors}.
  - Exactly one datapoint per real transport call (not per retry attempt).
  - Cache HITS emit NOTHING — the metric measures transport health, not call
    volume.
  - ctx.metrics needs no capability (available to all plugins), so the manifest
    is unchanged.

These contracts are immutable.
"""

from __future__ import annotations

import json

import plugin_main as p
from yourbot_sdk import RateLimitError, RpcTimeoutError
from yourbot_sdk.testing import MockContext


def _outcomes(ctx, source: str) -> list[str]:
    """All http.request outcomes recorded for one source, in order."""
    out = []
    for rec in ctx.metrics.recorded:
        if rec["metric"] == "http.request" and rec["tags"].get("source") == source:
            out.append(rec["tags"].get("outcome"))
    return out


# ── AniList ─────────────────────────────────────────────────────────────────


def test_anilist_ok_records_one_ok():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": {"id": 1}}}))
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["ok"]


def test_anilist_non_2xx_records_non_2xx():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=404, body="nope")
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["non_2xx"]


def test_anilist_graphql_errors_records_api_errors():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"errors": [{"message": "bad"}]}))
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["api_errors"]


def test_anilist_unparseable_body_records_bad_body():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body="<html>nope</html>")
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["bad_body"]


def test_anilist_timeout_exhaustion_records_one_timeout():
    """A persistent timeout retries internally but emits a SINGLE datapoint."""
    ctx = MockContext()

    def _raise_post(*_a, **_k):
        raise RpcTimeoutError("simulated timeout")

    ctx.http.post = _raise_post  # type: ignore[assignment]
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["timeout"]


# ── Jikan ───────────────────────────────────────────────────────────────────


def test_jikan_ok_records_one_ok():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=200, body='{"data": [{"mal_id": 1}]}')
    p._jikan_query(ctx, "/anime", {"q": "x"})
    assert _outcomes(ctx, "jikan") == ["ok"]


def test_jikan_non_2xx_records_non_2xx():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=500, body="boom")
    p._jikan_query(ctx, "/anime", {"q": "x"})
    assert _outcomes(ctx, "jikan") == ["non_2xx"]


def test_jikan_unparseable_body_records_bad_body():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=200, body="[1, 2, 3]")  # list, not envelope
    p._jikan_query(ctx, "/anime", {"q": "x"})
    assert _outcomes(ctx, "jikan") == ["bad_body"]


def test_jikan_rate_limited_records_rate():
    ctx = MockContext()

    def _raise_get(*_a, **_k):
        raise RateLimitError("simulated rate limit")

    ctx.http.get = _raise_get  # type: ignore[assignment]
    p._jikan_query(ctx, "/anime", {"q": "x"})
    assert _outcomes(ctx, "jikan") == ["rate"]


# ── Kitsu ───────────────────────────────────────────────────────────────────


def test_kitsu_ok_records_one_ok():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=200, body='{"data": [{"id": "1"}]}')
    p._kitsu_query(ctx, "/anime", {"filter[text]": "x"})
    assert _outcomes(ctx, "kitsu") == ["ok"]


def test_kitsu_non_2xx_records_non_2xx():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=503, body="down")
    p._kitsu_query(ctx, "/anime", {"filter[text]": "x"})
    assert _outcomes(ctx, "kitsu") == ["non_2xx"]


def test_kitsu_unparseable_body_records_bad_body():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=200, body="not json")
    p._kitsu_query(ctx, "/anime", {"filter[text]": "x"})
    assert _outcomes(ctx, "kitsu") == ["bad_body"]


# ── shape + cache-hit contract ──────────────────────────────────────────────


def test_metric_name_and_value():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": {"id": 1}}}))
    p._anilist_query(ctx, "query { Media }", {})
    rec = ctx.metrics.recorded[0]
    assert rec["metric"] == "http.request"
    assert rec["value"] == 1.0
    assert set(rec["tags"]) == {"source", "outcome"}


def test_cache_hit_records_nothing():
    """First fetch records once; a cached repeat emits no datapoint."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": {"id": 1}}}))
    p._anilist_query(ctx, "query { Media }", {"v": 1}, cache=True)
    p._anilist_query(ctx, "query { Media }", {"v": 1}, cache=True)  # cache hit
    assert _outcomes(ctx, "anilist") == ["ok"]  # exactly one, not two
    assert len(ctx.http.requests) == 1  # sanity: second call never hit the wire
