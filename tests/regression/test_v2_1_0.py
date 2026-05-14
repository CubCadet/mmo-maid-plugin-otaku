"""Regression contract for otaku v2.1.0 — ratings.

IMMUTABLE — what shipped at v2.1.0:
- /rate <score> and /ratings [user] slash commands present in manifest.
- Score stored as SMALLINT in `otaku_user_anime.rating` column.
- Encoding: stored = round(score * 2); valid range 2..20 (i.e. 1.0–10.0).
- `_bootstrap_schema` runs ADD COLUMN IF NOT EXISTS rating SMALLINT.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_rate_and_ratings():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"rate", "ratings"}.issubset(names)


def test_rate_score_option_is_number_type():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "rate")
    score = next(o for o in cmd["options"] if o["name"] == "score")
    # Type 10 = NUMBER per Discord API.
    assert score["type"] == 10
    assert score["required"] is True


# ── Encoding ────────────────────────────────────────────────────────────────


def test_score_encoding_roundtrips():
    assert p._encode_rating(7.5) == 15
    assert p._encode_rating(10.0) == 20
    assert p._encode_rating(1.0) == 2
    assert p._format_rating(15) == "7.5"
    assert p._format_rating(20) == "10.0"


def test_score_encoding_rejects_out_of_range():
    assert p._encode_rating(0) is None
    assert p._encode_rating(10.5) is None
    assert p._encode_rating(-1) is None
    assert p._encode_rating("seven") is None


# ── Schema bootstrap includes rating column ─────────────────────────────────


def test_bootstrap_schema_adds_rating_column():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    assert any(
        "ADD COLUMN IF NOT EXISTS rating SMALLINT" in c["sql"]
        for c in ctx.sql.executed
    )


# ── /rate writes rating column ──────────────────────────────────────────────


SAMPLE = {
    "id": 555,
    "title": {"romaji": "Test Show", "english": ""},
    "description": "",
    "coverImage": {"large": ""},
    "bannerImage": None,
    "averageScore": 75,
    "popularity": 100,
    "format": "TV",
    "episodes": 12,
    "status": "FINISHED",
    "season": "SUMMER",
    "seasonYear": 2024,
    "genres": ["Action"],
    "siteUrl": "https://anilist.co/anime/555",
}


def test_rate_command_writes_encoded_rating():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-rate", 555, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media,
    # and media_type was added to the INSERT column list. Positional indexing
    # is no longer stable; assert on intent (rating-as-int*2 is in params).
    p.cmd_rate(ctx, _slash("rate", {"score": 9.0}, user_id="reg-rate"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    assert 18 in inserts[-1]["params"]  # 9.0 × 2


def test_ratings_query_orders_by_rating_desc():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return []

    ctx.sql.query = _q
    p.cmd_ratings(ctx, _slash("ratings", {}, user_id="reg-rs"))
    assert "ORDER BY rating DESC" in captured["sql"]
    assert "rating IS NOT NULL" in captured["sql"]
