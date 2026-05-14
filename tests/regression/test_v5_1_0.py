"""Regression contract for otaku v5.1.0 — /my-stats personal page.

IMMUTABLE — what shipped at v5.1.0:
- /my-stats slash command (no options, self-only — defers to caller).
- Adds three list sections on top of /stats: top rated, top favorites,
  recently completed. Each capped at 5 entries.
- One AniList batch HTTP call resolves titles for everything that lands
  in the embed.
- Empty users get a friendly empty-state pointer.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[],
        **extra,
    )


def test_manifest_includes_my_stats():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "my-stats" in names


def test_my_stats_limits_frozen():
    assert p.MY_STATS_TOP_RATED_LIMIT == 5
    assert p.MY_STATS_TOP_FAVORITES_LIMIT == 5
    assert p.MY_STATS_RECENT_COMPLETED_LIMIT == 5


def test_my_stats_top_rated_orders_by_rating_desc():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return []

    ctx.sql.query = _q
    p._my_stats_top_rated(ctx, "u")
    assert "ORDER BY rating DESC" in captured["sql"]
    assert "rating IS NOT NULL" in captured["sql"]


def test_my_stats_top_favorites_filters_is_favorite_true():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return []

    ctx.sql.query = _q
    p._my_stats_top_favorites(ctx, "u")
    assert "is_favorite = TRUE" in captured["sql"]


def test_my_stats_recent_completed_filters_status():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return []

    ctx.sql.query = _q
    p._my_stats_recently_completed(ctx, "u")
    assert "status = 'completed'" in captured["sql"]


def test_my_stats_empty_state_when_no_rows():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    p.cmd_my_stats(ctx, _slash("my-stats", user_id="empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "haven't" in (follow.get("content") or "")
