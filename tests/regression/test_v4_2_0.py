"""Regression contract for otaku v4.2.0 — seasonal premieres.

IMMUTABLE — what shipped at v4.2.0:
- /season-premieres slash command with optional season + year options.
  Defaults to next season when neither arg is passed.
- Pagination buttons use `otaku:premieres:<season>:<year>:<page>`.
- _next_season(), _current_season_at(), _season_is_fresh() helpers
  define the season boundaries.
- The hourly cron also calls _dispatch_premieres_digest(), which posts
  a seasonal-premieres digest to the announcement channel during the
  first PREMIERES_DIGEST_WINDOW_DAYS (7) of each season. Dedup per
  season per server via KV at PREMIERES_DIGEST_KV.
"""
from __future__ import annotations

import datetime as dt
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


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_season_premieres():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "season-premieres" in names


def test_season_premieres_options_are_optional():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "season-premieres")
    for opt in cmd.get("options", []):
        assert opt.get("required") is not True


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_next_season_wraps_after_fall():
    fixed = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
    assert p._next_season(fixed) == ("WINTER", 2027)


def test_next_season_inside_winter_returns_spring():
    fixed = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    assert p._next_season(fixed) == ("SPRING", 2026)


def test_season_is_fresh_constants_frozen():
    assert p.PREMIERES_DIGEST_WINDOW_DAYS == 7
    assert p.PREMIERES_DIGEST_KV == "premieres_digest_last:guild"


def test_season_is_fresh_inside_first_week():
    assert p._season_is_fresh(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)) is True
    assert p._season_is_fresh(dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)) is False


# ── Pagination contract ─────────────────────────────────────────────────────


def test_premieres_pagination_custom_id_format():
    cid = "otaku:premieres:SPRING:2027:3"
    parts = cid.split(":")
    assert parts[:3] == ["otaku", "premieres", "SPRING"]
    assert parts[3] == "2027" and parts[4] == "3"


def test_premieres_pagination_button_renders_correct_page():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {
        "Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 4},
            "media": [{"id": 1, "title": {"romaji": "x", "english": ""},
                       "description": "", "coverImage": {"large": ""},
                       "bannerImage": None, "averageScore": 0, "popularity": 0,
                       "format": "TV", "episodes": 12, "status": "RELEASING",
                       "season": "SUMMER", "seasonYear": 2026, "genres": [],
                       "siteUrl": "https://anilist.co/anime/1"}],
        },
    }}))

    p._route_components(ctx, _component("otaku:premieres:SUMMER:2026:4", user_id="u"))

    follow = ctx.interaction.followups[-1]
    assert "Summer 2026" in follow["embeds"][0]["title"]


# ── Digest dedup ────────────────────────────────────────────────────────────


def test_dispatch_digest_dedups_once_per_season(monkeypatch):
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "ch")
    monkeypatch.setattr(p, "_season_is_fresh", lambda *_a, **_k: True)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {
        "Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 1},
            "media": [{"id": 1, "title": {"romaji": "x", "english": ""},
                       "description": "", "coverImage": {"large": ""},
                       "bannerImage": None, "averageScore": 0, "popularity": 0,
                       "format": "TV", "episodes": 12, "status": "RELEASING",
                       "season": "SUMMER", "seasonYear": 2026, "genres": [],
                       "siteUrl": "https://anilist.co/anime/1"}],
        },
    }}))

    assert p._dispatch_premieres_digest(ctx) is True
    assert p._dispatch_premieres_digest(ctx) is False  # dedup
    assert len(ctx.discord.messages_sent) == 1
