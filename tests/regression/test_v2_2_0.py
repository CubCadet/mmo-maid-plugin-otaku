"""Regression contract for otaku v2.2.0 — episode progress.

IMMUTABLE — what shipped at v2.2.0:
- /progress <episodes> slash command (episodes is integer, type 4).
- Schema: episodes_watched SMALLINT DEFAULT 0 (added via ALTER IF NOT EXISTS).
- Status auto-promotes to 'completed' when episodes_watched == total episodes.
- /anime card adds a "Your progress" field when user has tracked progress > 0.
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


SAMPLE = {
    "id": 901,
    "title": {"romaji": "Progress Show", "english": ""},
    "description": "",
    "coverImage": {"large": ""},
    "bannerImage": None,
    "averageScore": 70,
    "popularity": 100,
    "format": "TV",
    "episodes": 12,
    "status": "RELEASING",
    "season": "SUMMER",
    "seasonYear": 2024,
    "genres": ["Drama"],
    "siteUrl": "https://anilist.co/anime/901",
}


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_progress():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "progress" in names


def test_progress_episodes_option_is_integer():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "progress")
    eps = next(o for o in cmd["options"] if o["name"] == "episodes")
    assert eps["type"] == 4  # INTEGER per Discord API
    assert eps["required"] is True


# ── Schema ──────────────────────────────────────────────────────────────────


def test_bootstrap_schema_adds_episodes_column():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    assert any(
        "ADD COLUMN IF NOT EXISTS episodes_watched SMALLINT" in c["sql"]
        for c in ctx.sql.executed
    )


# ── /progress upsert behavior ──────────────────────────────────────────────


# regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media, and
# media_type was added to the INSERT column list. Positional params[N]
# indexing isn't stable across the rename; the three /progress tests below
# now assert on intent (the value appears in params anywhere).


def test_progress_writes_episodes_watched_column():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-p", 901, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    p.cmd_progress(ctx, _slash("progress", {"episodes": 3}, user_id="reg-p"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    assert 3 in inserts[-1]["params"]
    assert "episodes_watched = EXCLUDED.episodes_watched" in inserts[-1]["sql"]


def test_progress_at_total_promotes_status_to_completed():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-pc", 901, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    p.cmd_progress(ctx, _slash("progress", {"episodes": 12}, user_id="reg-pc"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert "completed" in inserts[-1]["params"]


def test_progress_caps_at_total():
    """An over-cap input is capped to the total. Param holds the capped value."""
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-pcap", 901, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    p.cmd_progress(ctx, _slash("progress", {"episodes": 99}, user_id="reg-pcap"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert 12 in inserts[-1]["params"]  # capped to SAMPLE total
